package com.topjohnwu.magisk.update;

import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import android.system.ErrnoException;
import android.system.Os;
import android.util.Base64;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.util.Map;

public final class UpdateClient {
    public interface Progress { void update(long current, long total); }
    public interface Connections { HttpURLConnection open(URL url) throws IOException; }
    public interface Clock { long seconds(); }

    private static final Object STATE_LOCK = new Object();
    private final Context context;
    private final String repository;
    public final String applicationId;
    public final String defaultChannel;
    public final String metadataBase;
    private final byte[] certificate;
    private final String localCertificate;
    private final int minimumCode;
    private final File directory;
    private final Connections connections;
    private final Clock clock;

    public static UpdateClient create(Context context, int minimumCode) throws IOException {
        return create(context, minimumCode, url -> (HttpURLConnection) url.openConnection());
    }

    public static UpdateClient create(Context context, int minimumCode, Connections connections) throws IOException {
        Map<String, Object> trust = UpdateMetadata.parse(UpdateMetadata.utf8(
                UpdateFiles.ownAsset(context, "update-trust.json", 16384)));
        byte[] local = UpdateFiles.ownAsset(context, "manager-cert.der", 8192);
        return new UpdateClient(context, trust, UpdateMetadata.sha256(local), minimumCode, connections);
    }

    public UpdateClient(Context context, Map<String, Object> trust, String localCertificate,
                        int minimumCode, Connections connections) throws IOException {
        this(context, trust, localCertificate, minimumCode, connections, () -> System.currentTimeMillis() / 1000);
    }

    public UpdateClient(Context context, Map<String, Object> trust, String localCertificate,
                        int minimumCode, Connections connections, Clock clock) throws IOException {
        this.context = context;
        UpdateMetadata.keys(trust, "schema", "repository", "application_id", "default_channel",
                "metadata_base", "certificate_sha256", "certificate");
        UpdateMetadata.number(trust, "schema", 1, 1);
        repository = UpdateMetadata.string(trust, "repository", 128);
        if (!repository.matches("[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")) throw new IOException("Invalid update repository");
        applicationId = UpdateMetadata.token(trust, "application_id");
        defaultChannel = UpdateMetadata.token(trust, "default_channel");
        metadataBase = UpdateMetadata.string(trust, "metadata_base", 2048);
        certificate = Base64.decode(UpdateMetadata.string(trust, "certificate", 8192), Base64.NO_WRAP);
        if (!UpdateMetadata.sha256(certificate).equals(UpdateMetadata.hex(trust, "certificate_sha256", 64)))
            throw new IOException("Invalid update trust root");
        this.localCertificate = localCertificate;
        this.minimumCode = minimumCode;
        this.connections = connections;
        this.clock = clock;
        Context storage = Build.VERSION.SDK_INT >= 24 ? context.createDeviceProtectedStorageContext() : context;
        directory = new File(storage.getFilesDir(), "updates");
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory())
            throw new IOException("Cannot create update state directory");
        try {
            if (!android.system.OsConstants.S_ISDIR(Os.lstat(directory.getPath()).st_mode))
                throw new IOException("Redirected update state directory");
            Os.chmod(directory.getPath(), 0700);
        } catch (ErrnoException e) {
            throw new IOException(e);
        }
    }

    public static String channelForUrl(String url) throws IOException {
        String path = uri(url).getPath();
        for (String channel : new String[]{"stable", "beta", "canary"}) {
            if (path.endsWith("/" + channel + ".json")) return channel;
        }
        throw new IOException("Update URL must select a stable, beta or canary manifest");
    }

    public UpdateMetadata fetch(String url, String channel) throws IOException {
        HttpURLConnection connection = open(url, false);
        try {
            return accept(UpdateMetadata.utf8(UpdateFiles.read(connection.getInputStream(), UpdateMetadata.MAX_METADATA,
                    SystemClock.elapsedRealtime() + 60000)), channel);
        } finally {
            connection.disconnect();
        }
    }

    public UpdateMetadata accept(String envelope, String channel) throws IOException {
        long now = clock.seconds();
        UpdateMetadata metadata = UpdateMetadata.verify(envelope, certificate, channel, applicationId, now, minimumCode);
        if (!localCertificate.equals(metadata.fields.get("certificate_sha256")))
            throw new IOException("This installation uses a different signing identity; a verified reinstall is required");
        artifactUrl(metadata.manager.url, (String) metadata.fields.get("tag"));
        artifactUrl(metadata.stub.url, (String) metadata.fields.get("tag"));
        synchronized (STATE_LOCK) {
            try (RandomAccessFile lock = new RandomAccessFile(new File(directory, "state.lock"), "rw");
                 var ignored = lock.getChannel().lock()) {
                File file = new File(directory, "state.json");
                JSONObject state;
                if (file.exists()) {
                    UpdateFiles.regular(file);
                    Map<String, Object> saved = UpdateMetadata.parse(UpdateMetadata.utf8(
                            UpdateFiles.read(new FileInputStream(file), 16384)));
                    if (saved.size() != 10) throw new IOException("Missing or unknown update state fields");
                    UpdateMetadata.number(saved, "schema", 1, 1);
                    UpdateMetadata.number(saved, "clock", 0, Long.MAX_VALUE - UpdateMetadata.CLOCK_SKEW);
                    UpdateMetadata.number(saved, "highest_code", 0, 2100000000);
                    if (!"".equals(saved.get("highest_hash"))) UpdateMetadata.hex(saved, "highest_hash", 64);
                    for (String name : new String[]{"stable", "beta", "canary"}) {
                        long savedSequence = UpdateMetadata.number(saved, name + "_sequence", 0, Long.MAX_VALUE);
                        if (savedSequence == 0 ? !"".equals(saved.get(name + "_hash")) :
                                !UpdateMetadata.hex(saved, name + "_hash", 64).matches("[0-9a-f]{64}"))
                            throw new IOException("Invalid saved update hash");
                    }
                    state = new JSONObject(saved);
                } else {
                    state = new JSONObject().put("schema", 1).put("clock", 0).put("highest_code", minimumCode).put("highest_hash", "");
                    for (String name : new String[]{"stable", "beta", "canary"})
                        state.put(name + "_sequence", 0).put(name + "_hash", "");
                }
                if (now + UpdateMetadata.CLOCK_SKEW < state.optLong("clock", 0))
                    throw new IOException("Device clock moved behind the accepted update state");
                if (metadata.versionCode < state.optInt("highest_code", minimumCode))
                    throw new IOException("Update would roll back an accepted release");
                if (metadata.versionCode == state.optInt("highest_code", minimumCode) &&
                        !state.optString("highest_hash").isEmpty() && !metadata.manager.sha256.equals(state.optString("highest_hash")))
                    throw new IOException("Conflicting APKs use the same upgrade code");
                long sequence = state.optLong(channel + "_sequence", 0);
                if (metadata.sequence < sequence || (metadata.sequence == sequence &&
                        !metadata.payloadHash.equals(state.optString(channel + "_hash"))))
                    throw new IOException("Replayed or conflicting update metadata");
                state.put("clock", Math.max(now, state.optLong("clock", 0)));
                state.put("highest_code", metadata.versionCode);
                state.put("highest_hash", metadata.manager.sha256);
                state.put(channel + "_sequence", metadata.sequence);
                state.put(channel + "_hash", metadata.payloadHash);
                File temporary = File.createTempFile("state-", ".part", directory);
                try {
                    try (FileOutputStream out = new FileOutputStream(temporary)) {
                        out.write(state.toString().getBytes(StandardCharsets.UTF_8));
                        out.getFD().sync();
                    }
                    UpdateFiles.publish(temporary, file);
                } finally {
                    temporary.delete();
                }
            } catch (JSONException e) {
                throw new IOException("Invalid saved update state; refusing to reset rollback protection", e);
            }
        }
        return metadata;
    }

    public File download(UpdateMetadata metadata, File destination, Progress progress) throws IOException {
        // Recheck the signed envelope, expiry and rollback state at the actual write boundary.
        metadata = accept(metadata.envelope, metadata.channel);
        File parent = destination.getParentFile();
        if (!parent.isDirectory() && !parent.mkdirs()) throw new IOException("Cannot create download directory");
        File temporary = File.createTempFile("manager-", ".part", parent);
        HttpURLConnection connection = null;
        try {
            connection = open(metadata.manager.url, true);
            long declared = connection.getContentLength();
            if (declared >= 0 && declared != metadata.manager.size) throw new IOException("Wrong APK Content-Length");
            MessageDigest hash = MessageDigest.getInstance("SHA-256");
            long count = 0;
            long deadline = SystemClock.elapsedRealtime() + 10 * 60 * 1000;
            try (InputStream in = connection.getInputStream(); FileOutputStream out = new FileOutputStream(temporary)) {
                byte[] buffer = new byte[65536];
                int n;
                while ((n = in.read(buffer)) != -1) {
                    if (Thread.currentThread().isInterrupted() || SystemClock.elapsedRealtime() > deadline)
                        throw new InterruptedIOException("Update download interrupted or timed out");
                    count += n;
                    if (count > metadata.manager.size) throw new IOException("APK is larger than signed metadata");
                    out.write(buffer, 0, n);
                    hash.update(buffer, 0, n);
                    if (progress != null) progress.update(count, metadata.manager.size);
                }
                out.getFD().sync();
            }
            if (count != metadata.manager.size || !UpdateMetadata.toHex(hash.digest()).equals(metadata.manager.sha256))
                throw new IOException("Incomplete or tampered APK download");
            metadata.checkTime(clock.seconds());
            UpdateFiles.verifyManager(context, temporary, applicationId, minimumCode, localCertificate, metadata);
            accept(metadata.envelope, metadata.channel);
            UpdateFiles.publish(temporary, destination);
            return destination;
        } catch (GeneralSecurityException e) {
            throw new IOException(e);
        } finally {
            if (connection != null) connection.disconnect();
            temporary.delete();
        }
    }

    private void artifactUrl(String url, String tag) throws IOException {
        URI uri = uri(url);
        String prefix = "/" + repository + "/releases/download/" + tag + "/";
        if (!"github.com".equals(uri.getHost()) || !uri.getRawPath().startsWith(prefix) ||
                !uri.getRawPath().substring(prefix.length()).matches("[a-zA-Z0-9._-]+\\.apk") || uri.getRawQuery() != null)
            throw new IOException("APK URL must name an immutable asset in the project release");
    }

    private static URI uri(String value) throws IOException {
        try {
            URI uri = new URI(value);
            if (!"https".equals(uri.getScheme()) || uri.getHost() == null ||
                    uri.getUserInfo() != null || uri.getFragment() != null ||
                    (uri.getPort() != -1 && uri.getPort() != 443) || !uri.normalize().equals(uri))
                throw new IOException("Updates require a plain HTTPS URL");
            return uri;
        } catch (URISyntaxException e) {
            throw new IOException("Invalid update URL", e);
        }
    }

    private HttpURLConnection open(String address, boolean artifact) throws IOException {
        URI initial = uri(address);
        String host = initial.getHost();
        for (int redirects = 0; redirects <= 3; ++redirects) {
            URI target = uri(address);
            if (artifact ? !(target.getHost().equals("github.com") || target.getHost().equals("release-assets.githubusercontent.com"))
                    : !target.getHost().equals(host))
                throw new IOException("Update redirect changed its allowed origin");
            HttpURLConnection connection = connections.open(target.toURL());
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(20000);
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept-Encoding", "identity");
            connection.setRequestProperty("Cache-Control", "no-cache");
            try {
                int status = connection.getResponseCode();
                if (status == 200) {
                    String encoding = connection.getContentEncoding();
                    if (encoding != null && !encoding.equalsIgnoreCase("identity"))
                        throw new IOException("Unexpected update content encoding");
                    return connection;
                }
                if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                    String location = connection.getHeaderField("Location");
                    if (location == null) throw new IOException("Missing update redirect target");
                    address = target.resolve(location).toString();
                } else {
                    throw new IOException("Update service returned HTTP " + status);
                }
            } catch (IOException | RuntimeException e) {
                connection.disconnect();
                throw e;
            }
            connection.disconnect();
        }
        throw new IOException("Too many update redirects");
    }
}
