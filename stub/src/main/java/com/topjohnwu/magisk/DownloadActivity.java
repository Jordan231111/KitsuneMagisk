package com.topjohnwu.magisk;

import static android.R.string.no;
import static android.R.string.ok;
import static android.R.string.yes;
import static com.topjohnwu.magisk.R.string.dling;
import static com.topjohnwu.magisk.R.string.no_internet_msg;
import static com.topjohnwu.magisk.R.string.open_project;
import static com.topjohnwu.magisk.R.string.update_service_unavailable;
import static com.topjohnwu.magisk.R.string.upgrade_msg;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.ProgressDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.res.loader.ResourcesLoader;
import android.content.res.loader.ResourcesProvider;
import android.net.Uri;
import android.os.AsyncTask;
import android.os.Build;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.system.Os;
import android.system.OsConstants;
import android.util.Log;
import android.view.ContextThemeWrapper;

import com.topjohnwu.magisk.net.Networking;
import com.topjohnwu.magisk.net.Request;
import com.topjohnwu.magisk.utils.APKInstall;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.zip.InflaterInputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;

import javax.crypto.Cipher;
import javax.crypto.CipherInputStream;
import javax.crypto.SecretKey;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

public class DownloadActivity extends Activity {

    private static final String APP_NAME = "Kitsune Mask";
    private String apkLink = BuildConfig.APK_URL;
    private Context themed;
    private ProgressDialog dialog;
    private boolean dynLoad;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (DynLoad.activeClassLoader instanceof AppClassLoader) {
            // For some reason activity is created before Application.attach(),
            // relaunch the activity using the same intent
            finishAffinity();
            startActivity(getIntent());
            return;
        }

        themed = new ContextThemeWrapper(this, android.R.style.Theme_DeviceDefault);

        // Only download and dynamic load full APK if hidden
        dynLoad = !getPackageName().equals(BuildConfig.APPLICATION_ID);

        // Inject resources
        try {
            loadResources();
        } catch (Exception e) {
            error(e);
        }

        ProviderInstaller.install(this);

        if (!BuildConfig.UPDATE_SERVICE_CONFIGURED ||
                BuildConfig.APK_URL == null || BuildConfig.APK_SHA256 == null) {
            showUpdateUnavailable();
        } else if (Networking.checkNetworkStatus(this)) {
            showDialog();
        } else {
            new AlertDialog.Builder(themed)
                    .setCancelable(false)
                    .setTitle(APP_NAME)
                    .setMessage(getString(no_internet_msg))
                    .setNegativeButton(ok, (d, w) -> finish())
                    .show();
        }
    }

    @Override
    public void finish() {
        super.finish();
        Runtime.getRuntime().exit(0);
    }

    private void error(Throwable e) {
        Log.e(getClass().getSimpleName(), Log.getStackTraceString(e));
        runOnUiThread(this::finish);
    }

    private Request request(String url) {
        return Networking.get(url).setErrorHandler((conn, e) -> error(e));
    }

    private void showDialog() {
        new AlertDialog.Builder(themed)
                .setCancelable(false)
                .setTitle(APP_NAME)
                .setMessage(getString(upgrade_msg))
                .setPositiveButton(yes, (d, w) -> dlAPK())
                .setNegativeButton(no, (d, w) -> finish())
                .show();
    }

    private void showUpdateUnavailable() {
        new AlertDialog.Builder(themed)
                .setCancelable(false)
                .setTitle(APP_NAME)
                .setMessage(getString(update_service_unavailable))
                .setPositiveButton(open_project, (d, w) -> {
                    try {
                        Intent browser = Intent.makeMainSelectorActivity(
                                Intent.ACTION_MAIN, Intent.CATEGORY_APP_BROWSER);
                        browser.setData(Uri.parse(BuildConfig.PROJECT_URL));
                        startActivity(browser);
                    } catch (ActivityNotFoundException e) {
                        Log.w(getClass().getSimpleName(), "No browser can open the project page", e);
                    } finally {
                        finish();
                    }
                })
                .setNegativeButton(ok, (d, w) -> finish())
                .show();
    }

    private void dlAPK() {
        dialog = ProgressDialog.show(themed, getString(dling), getString(dling) + " " + APP_NAME, true);
        // Download into a non-loadable staging name. Only a complete artifact
        // matching the build-pinned digest may reach PackageInstaller or dyn/.
        var request = request(apkLink).setExecutor(AsyncTask.THREAD_POOL_EXECUTOR);
        File current = StubApk.current(this);
        final File staging;
        try {
            staging = File.createTempFile("download-", ".apk", current.getParentFile());
        } catch (IOException e) {
            error(e);
            return;
        }
        request.setErrorHandler((conn, e) -> {
            staging.delete();
            error(e);
        });
        request.getAsFile(staging, file -> {
            try {
                if (!APKInstall.matchesSha256(file, BuildConfig.APK_SHA256))
                    throw new IOException("Downloaded APK failed SHA-256 verification");
                if (dynLoad) {
                    Os.rename(file.getPath(), StubApk.update(this).getPath());
                    runOnUiThread(() -> StubApk.restartProcess(this));
                    return;
                }
                var session = APKInstall.startSession(this);
                try (var input = new java.io.FileInputStream(file)) {
                    session.write(input);
                }
                Intent intent = session.waitIntent();
                if (intent != null) {
                    runOnUiThread(() -> {
                        startActivity(intent);
                        finish();
                    });
                } else if (session.isSuccessful()) {
                    runOnUiThread(this::finish);
                } else {
                    throw new IOException("Package installer did not accept the verified APK");
                }
            } catch (Exception e) {
                error(e);
            } finally {
                file.delete();
            }
        });
    }

    private void decryptResources(OutputStream out) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/CBC/PKCS5Padding");
        SecretKey key = new SecretKeySpec(Bytes.key(), "AES");
        IvParameterSpec iv = new IvParameterSpec(Bytes.iv());
        cipher.init(Cipher.DECRYPT_MODE, key, iv);
        var is = new InflaterInputStream(new CipherInputStream(
                new ByteArrayInputStream(Bytes.res()), cipher));
        try (is; out) {
            APKInstall.transfer(is, out);
        }
    }

    private void loadResources() throws Exception {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            var fd = Os.memfd_create("res", 0);
            try {
                decryptResources(new FileOutputStream(fd));
                Os.lseek(fd, 0, OsConstants.SEEK_SET);
                var loader = new ResourcesLoader();
                try (var pfd = ParcelFileDescriptor.dup(fd)) {
                    loader.addProvider(ResourcesProvider.loadFromTable(pfd, null));
                    getResources().addLoaders(loader);
                }
            } finally {
                Os.close(fd);
            }
        } else {
            File res = new File(getCodeCacheDir(), "res.apk");
            try (var out = new ZipOutputStream(new FileOutputStream(res))) {
                // AndroidManifest.xml is reuqired on Android 6-, and directory support is broken on Android 9-10
                out.putNextEntry(new ZipEntry("AndroidManifest.xml"));
                try (var stubApk = new ZipFile(getPackageCodePath())) {
                    APKInstall.transfer(stubApk.getInputStream(stubApk.getEntry("AndroidManifest.xml")), out);
                }
                out.putNextEntry(new ZipEntry("resources.arsc"));
                decryptResources(out);
            }
            StubApk.addAssetPath(getResources(), res.getPath());
        }
    }
}
