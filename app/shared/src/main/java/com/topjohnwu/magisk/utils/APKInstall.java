package com.topjohnwu.magisk.utils;

import static android.content.pm.PackageInstaller.EXTRA_SESSION_ID;
import static android.content.pm.PackageInstaller.EXTRA_STATUS;
import static android.content.pm.PackageInstaller.STATUS_FAILURE_INVALID;
import static android.content.pm.PackageInstaller.STATUS_PENDING_USER_ACTION;
import static android.content.pm.PackageInstaller.STATUS_SUCCESS;

import android.annotation.SuppressLint;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageInstaller.SessionParams;
import android.os.Build;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public final class APKInstall {

    private static final long INSTALL_RESULT_TIMEOUT_SECONDS = 30;

    public static void transfer(InputStream in, OutputStream out) throws IOException {
        int size = 8192;
        var buffer = new byte[size];
        int read;
        while ((read = in.read(buffer, 0, size)) >= 0) {
            out.write(buffer, 0, read);
        }
    }

    public static boolean matchesSha256(byte[] actual, String expected) {
        if (actual == null || actual.length != 32 || expected == null || expected.length() != 64)
            return false;
        byte[] expectedBytes = new byte[32];
        for (int i = 0; i < expectedBytes.length; ++i) {
            int high = Character.digit(expected.charAt(i * 2), 16);
            int low = Character.digit(expected.charAt(i * 2 + 1), 16);
            if (high < 0 || low < 0)
                return false;
            expectedBytes[i] = (byte) ((high << 4) | low);
        }
        return MessageDigest.isEqual(expectedBytes, actual);
    }

    public static boolean matchesSha256(File file, String expected) throws IOException {
        if (expected == null || expected.length() != 64)
            return false;
        final MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new AssertionError("Android runtime lacks SHA-256", e);
        }
        try (InputStream in = new DigestInputStream(new FileInputStream(file), digest)) {
            byte[] buffer = new byte[8192];
            while (in.read(buffer) >= 0) { }
        }
        return matchesSha256(digest.digest(), expected);
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    public static void registerReceiver(
            Context context, BroadcastReceiver receiver, IntentFilter filter) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // noinspection InlinedApi
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            // Android 13 is the first release that requires an explicit
            // exported state for dynamically registered non-system receivers.
            context.registerReceiver(receiver, filter);
        }
    }

    public static Session startSession(Context context) {
        return startSession(context, null, null);
    }

    public static Session startSession(
            Context context, Runnable onSuccess, Runnable onFailure) {
        context = context.getApplicationContext();
        var receiver = new InstallReceiver(context, onSuccess, onFailure);
        try {
            registerReceiver(context, receiver, new IntentFilter(receiver.sessionId));
        } catch (RuntimeException e) {
            receiver.unregister();
            throw e;
        }
        return receiver;
    }

    @SuppressWarnings("deprecation")
    private static Intent getUserAction(Intent intent) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class);
        }
        return intent.getParcelableExtra(Intent.EXTRA_INTENT);
    }

    public interface Session {
        // @WorkerThread
        void write(InputStream in) throws IOException;
        // @WorkerThread @Nullable
        Intent waitIntent();
        boolean isSuccessful();
    }

    private static class InstallReceiver extends BroadcastReceiver implements Session {
        private final Context context;
        private final Runnable onSuccess;
        private final Runnable onFailure;
        private final CountDownLatch latch = new CountDownLatch(1);
        private final AtomicBoolean terminal = new AtomicBoolean(false);
        private Intent userAction = null;
        private volatile boolean successful = false;
        private volatile int installerSessionId = -1;

        final String sessionId = UUID.randomUUID().toString();

        private InstallReceiver(Context context, Runnable onSuccess, Runnable onFailure) {
            this.context = context;
            this.onSuccess = onSuccess;
            this.onFailure = onFailure;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            if (sessionId.equals(intent.getAction())) {
                int status = intent.getIntExtra(EXTRA_STATUS, STATUS_FAILURE_INVALID);
                switch (status) {
                    case STATUS_PENDING_USER_ACTION -> {
                        userAction = getUserAction(intent);
                        if (userAction == null) {
                            abandon(intent.getIntExtra(EXTRA_SESSION_ID, installerSessionId));
                            onFailure();
                        } else {
                            latch.countDown();
                        }
                    }
                    case STATUS_SUCCESS -> onSuccess();
                    default -> {
                        int id = intent.getIntExtra(EXTRA_SESSION_ID, installerSessionId);
                        abandon(id);
                        onFailure();
                    }
                }
            }
        }

        private void onSuccess() {
            if (!terminal.compareAndSet(false, true))
                return;
            successful = true;
            installerSessionId = -1;
            try {
                if (onSuccess != null)
                    onSuccess.run();
            } finally {
                unregister();
                latch.countDown();
            }
        }

        private void onFailure() {
            if (!terminal.compareAndSet(false, true))
                return;
            userAction = null;
            try {
                if (onFailure != null)
                    onFailure.run();
            } finally {
                unregister();
                latch.countDown();
            }
        }

        private void unregister() {
            try {
                context.unregisterReceiver(this);
            } catch (IllegalArgumentException ignored) {
            }
        }

        private void abandon(int id) {
            if (id < 0)
                return;
            try {
                context.getPackageManager().getPackageInstaller().abandonSession(id);
            } catch (SecurityException | IllegalArgumentException ignored) {
            } finally {
                installerSessionId = -1;
            }
        }

        @Override
        public Intent waitIntent() {
            boolean completed = false;
            try {
                completed = latch.await(INSTALL_RESULT_TIMEOUT_SECONDS, TimeUnit.SECONDS);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            if (!completed) {
                abandon(installerSessionId);
                onFailure();
            }
            return userAction;
        }

        @Override
        public boolean isSuccessful() {
            return successful;
        }

        @Override
        public void write(InputStream in) throws IOException {
            // noinspection InlinedApi
            var flag = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE;
            var intent = new Intent(sessionId).setPackage(context.getPackageName());
            var pending = PendingIntent.getBroadcast(context, 0, intent, flag);

            var installer = context.getPackageManager().getPackageInstaller();
            var params = new SessionParams(SessionParams.MODE_FULL_INSTALL);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                params.setRequireUserAction(SessionParams.USER_ACTION_NOT_REQUIRED);
            }
            int id = -1;
            boolean committed = false;
            try {
                id = installer.createSession(params);
                installerSessionId = id;
                try (var session = installer.openSession(id)) {
                    try (var out = session.openWrite(sessionId, 0, -1)) {
                        transfer(in, out);
                        session.fsync(out);
                    }
                    session.commit(pending.getIntentSender());
                    committed = true;
                }
            } finally {
                if (!committed) {
                    abandon(id);
                    onFailure();
                }
            }
        }
    }
}
