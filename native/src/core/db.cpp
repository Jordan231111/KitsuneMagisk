#include <unistd.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <cerrno>
#include <cstdlib>
#include <cstring>

#include <consts.hpp>
#include <base.hpp>
#include <db.hpp>
#include <core.hpp>

#include "db_migrations.hpp"

// Do not raise this for the hide-table union migration. Published v12 daemons
// destructively rebuild databases with a newer user_version. New schema work is
// tracked by an idempotent, validated marker row instead.
#define DB_VERSION 12

using namespace std;

struct sqlite3;
struct sqlite3_backup;

static sqlite3 *mDB = nullptr;

#define DBLOGV(...)
//#define DBLOGV(...) LOGD("magiskdb: " __VA_ARGS__)

// SQLite APIs

#define SQLITE_OPEN_READWRITE        0x00000002  /* Ok for sqlite3_open_v2() */
#define SQLITE_OPEN_READONLY         0x00000001  /* Ok for sqlite3_open_v2() */
#define SQLITE_OPEN_CREATE           0x00000004  /* Ok for sqlite3_open_v2() */
#define SQLITE_OPEN_FULLMUTEX        0x00010000  /* Ok for sqlite3_open_v2() */

static int (*sqlite3_open_v2)(
        const char *filename,
        sqlite3 **ppDb,
        int flags,
        const char *zVfs);
static const char *(*sqlite3_errmsg)(sqlite3 *db);
static int (*sqlite3_close)(sqlite3 *db);
static void (*sqlite3_free)(void *v);
static char *(*sqlite3_mprintf)(const char *format, ...);
static int (*sqlite3_exec)(
        sqlite3 *db,
        const char *sql,
        int (*callback)(void*, int, char**, char**),
        void *v,
        char **errmsg);
static sqlite3_backup *(*sqlite3_backup_init)(
        sqlite3 *pDest,
        const char *zDestName,
        sqlite3 *pSource,
        const char *zSourceName);
static int (*sqlite3_backup_step)(sqlite3_backup *p, int nPage);
static int (*sqlite3_backup_finish)(sqlite3_backup *p);
static int (*sqlite3_get_autocommit)(sqlite3 *db);

// Public database helpers return SQLite-owned error strings. Keep custom
// filesystem/migration errors on the same allocator once SQLite is available;
// db_err() can then release every returned error deterministically.
static bool sqlite_error_allocator = false;

static char *db_strdup(const char *message) {
    return sqlite_error_allocator ? sqlite3_mprintf("%s", message) : strdup(message);
}

// Internal Android linker APIs

static void (*android_get_LD_LIBRARY_PATH)(char *buffer, size_t buffer_size);
static void (*android_update_LD_LIBRARY_PATH)(const char *ld_library_path);

#define DLERR(ptr) if (!(ptr)) { \
    LOGE("db: %s\n", dlerror()); \
    return false; \
}

#define DLOAD(handle, arg) {\
    auto f = dlsym(handle, #arg); \
    DLERR(f) \
    *(void **) &(arg) = f; \
}

#ifdef __LP64__
constexpr char apex_path[] = "/apex/com.android.runtime/lib64:/apex/com.android.art/lib64:/apex/com.android.i18n/lib64:";
#else
constexpr char apex_path[] = "/apex/com.android.runtime/lib:/apex/com.android.art/lib:/apex/com.android.i18n/lib:";
#endif

static int dl_init = 0;

static bool dload_sqlite() {
    if (dl_init)
        return dl_init > 0;
    dl_init = -1;

    auto sqlite = dlopen("libsqlite.so", RTLD_LAZY);
    if (!sqlite) {
        // Should only happen on Android 10+
        auto dl = dlopen("libdl_android.so", RTLD_LAZY);
        DLERR(dl);

        DLOAD(dl, android_get_LD_LIBRARY_PATH);
        DLOAD(dl, android_update_LD_LIBRARY_PATH);

        // Inject APEX into LD_LIBRARY_PATH
        char ld_path[4096];
        memcpy(ld_path, apex_path, sizeof(apex_path));
        constexpr int len = sizeof(apex_path) - 1;
        android_get_LD_LIBRARY_PATH(ld_path + len, sizeof(ld_path) - len);
        android_update_LD_LIBRARY_PATH(ld_path);
        sqlite = dlopen("libsqlite.so", RTLD_LAZY);

        // Revert LD_LIBRARY_PATH just in case
        android_update_LD_LIBRARY_PATH(ld_path + len);
    }
    DLERR(sqlite);

    DLOAD(sqlite, sqlite3_open_v2);
    DLOAD(sqlite, sqlite3_errmsg);
    DLOAD(sqlite, sqlite3_close);
    DLOAD(sqlite, sqlite3_exec);
    DLOAD(sqlite, sqlite3_free);
    DLOAD(sqlite, sqlite3_mprintf);
    sqlite_error_allocator = true;
    DLOAD(sqlite, sqlite3_backup_init);
    DLOAD(sqlite, sqlite3_backup_step);
    DLOAD(sqlite, sqlite3_backup_finish);
    DLOAD(sqlite, sqlite3_get_autocommit);

    dl_init = 1;
    return true;
}

int db_strings::get_idx(string_view key) const {
    int idx = 0;
    for (const char *k : DB_STRING_KEYS) {
        if (key == k)
            break;
        ++idx;
    }
    return idx;
}

db_settings::db_settings() {
    // Default settings
    data[ROOT_ACCESS] = ROOT_ACCESS_APPS_AND_ADB;
    data[SU_BIOMETRIC] = BIOMETRIC_DISABLED;
    data[SU_MULTIUSER_MODE] = MULTIUSER_MODE_OWNER_ONLY;
    data[SU_MNT_NS] = NAMESPACE_MODE_REQUESTER;
    data[DENYLIST_CONFIG] = false;
    data[ZYGISK_CONFIG] = MagiskD::get()->is_emulator();
    data[SULIST_CONFIG] = false;
}

int db_settings::get_idx(string_view key) const {
    int idx = 0;
    for (const char *k : DB_SETTING_KEYS) {
        if (key == k)
            break;
        ++idx;
    }
    return idx;
}

static int ver_cb(void *ver, int, char **data, char **) {
    *((int *) ver) = parse_int(data[0]);
    return 0;
}

static int integrity_cb(void *valid, int count, char **data, char **) {
    *((bool *) valid) = count == 1 && data[0] && strcmp(data[0], "ok") == 0;
    return 0;
}

#define err_ret(e) if (e) return e;

static bool safe_backup_metadata(const struct stat &st, nlink_t links = 1) {
    return S_ISREG(st.st_mode) && st.st_uid == 0 && st.st_nlink == links
            && (st.st_mode & 0777) == 0600;
}

static char *sync_v12_backup(const char *path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0)
        return db_strdup(strerror(errno));
    struct stat st{};
    if (fstat(fd, &st) != 0) {
        char *error = db_strdup(strerror(errno));
        close(fd);
        return error;
    }
    if (!safe_backup_metadata(st)) {
        close(fd);
        return db_strdup("Unsafe v12 database backup metadata");
    }
    if (fsync(fd) != 0) {
        char *error = db_strdup(strerror(errno));
        close(fd);
        return error;
    }
    close(fd);

    int dir_fd = open(SECURE_DIR, O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW);
    if (dir_fd < 0)
        return db_strdup(strerror(errno));
    if (fsync(dir_fd) != 0) {
        char *error = db_strdup(strerror(errno));
        close(dir_fd);
        return error;
    }
    close(dir_fd);
    return nullptr;
}

static char *verify_v12_backup(const char *path) {
    char *sync_error = sync_v12_backup(path);
    if (sync_error)
        return sync_error;

    sqlite3 *backup = nullptr;
    int ret = sqlite3_open_v2(path, &backup,
            SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (ret) {
        char *error = db_strdup(backup ? sqlite3_errmsg(backup) : "Cannot open v12 database backup");
        if (backup)
            sqlite3_close(backup);
        return error;
    }

    int version = 0;
    bool valid = false;
    char *err = nullptr;
    sqlite3_exec(backup, "PRAGMA user_version", ver_cb, &version, &err);
    if (!err)
        sqlite3_exec(backup, "PRAGMA quick_check", integrity_cb, &valid, &err);
    if (err) {
        char *error = db_strdup(err);
        sqlite3_free(err);
        sqlite3_close(backup);
        return error;
    }
    sqlite3_close(backup);
    if (version != 12 || !valid)
        return db_strdup("Invalid v12 database backup");
    return nullptr;
}

static char *backup_v12_database(sqlite3 *source) {
    constexpr char backup_path[] = MAGISKDB ".v12.bak";
    constexpr char backup_tmp[] = MAGISKDB ".v12.bak.tmp";
    constexpr int SQLITE_DONE = 101;

    // Never overwrite the first pre-migration backup. It remains a byte-stable
    // recovery source even though rollback-safe databases keep user_version 12.
    struct stat backup_st{};
    if (lstat(backup_path, &backup_st) == 0) {
        // Recover the only multi-link state this function creates: power loss
        // after the no-replace link and before removal of the temporary name.
        struct stat temp_st{};
        if (lstat(backup_tmp, &temp_st) == 0
                && backup_st.st_dev == temp_st.st_dev
                && backup_st.st_ino == temp_st.st_ino) {
            if (!safe_backup_metadata(backup_st, 2)
                    || !safe_backup_metadata(temp_st, 2))
                return db_strdup("Unsafe interrupted v12 database backup metadata");
            if (unlink(backup_tmp) != 0)
                return db_strdup(strerror(errno));
        }
        return verify_v12_backup(backup_path);
    }
    if (errno != ENOENT)
        return db_strdup(strerror(errno));

    struct stat temp_st{};
    if (lstat(backup_tmp, &temp_st) == 0) {
        if (!safe_backup_metadata(temp_st))
            return db_strdup("Unsafe stale v12 database backup metadata");
        if (unlink(backup_tmp) != 0)
            return db_strdup(strerror(errno));
    } else if (errno != ENOENT) {
        return db_strdup(strerror(errno));
    }
    int temp_fd = open(backup_tmp,
            O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (temp_fd < 0)
        return db_strdup(strerror(errno));
    close(temp_fd);
    sqlite3 *destination = nullptr;
    int ret = sqlite3_open_v2(backup_tmp, &destination,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (ret) {
        char *error = db_strdup(destination ? sqlite3_errmsg(destination) : "Cannot create v12 database backup");
        if (destination)
            sqlite3_close(destination);
        unlink(backup_tmp);
        return error;
    }

    sqlite3_backup *backup = sqlite3_backup_init(destination, "main", source, "main");
    if (!backup) {
        char *error = db_strdup(sqlite3_errmsg(destination));
        sqlite3_close(destination);
        unlink(backup_tmp);
        return error;
    }

    int step = sqlite3_backup_step(backup, -1);
    int finish = sqlite3_backup_finish(backup);
    if (step != SQLITE_DONE || finish != 0) {
        char *error = db_strdup(sqlite3_errmsg(destination));
        sqlite3_close(destination);
        unlink(backup_tmp);
        return error;
    }
    sqlite3_close(destination);

    if (chmod(backup_tmp, 0600) != 0) {
        char *error = db_strdup(strerror(errno));
        unlink(backup_tmp);
        return error;
    }
    char *sync_error = sync_v12_backup(backup_tmp);
    if (sync_error) {
        unlink(backup_tmp);
        return sync_error;
    }
    // link(2) is an atomic no-replace publication within /data/adb. Unlike
    // rename(2), it cannot clobber a backup created between preflight and here.
    if (link(backup_tmp, backup_path) != 0) {
        int saved_errno = errno;
        unlink(backup_tmp);
        if (saved_errno == EEXIST)
            return verify_v12_backup(backup_path);
        return db_strdup(strerror(saved_errno));
    }
    if (unlink(backup_tmp) != 0) {
        // Both names intentionally remain linked to the same complete file.
        // The recovery branch above safely finishes this state on the next run.
        return db_strdup(strerror(errno));
    }
    return verify_v12_backup(backup_path);
}

static bool hide_migration_marker(sqlite3 *db, bool &exists, char *&error) {
    int tables = 0;
    sqlite3_exec(db,
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='hide_migration_v13'",
            ver_cb, &tables, &error);
    if (error || tables == 0) {
        exists = false;
        return false;
    }

    exists = true;
    int rows = 0;
    int valid_rows = 0;
    sqlite3_exec(db, "SELECT COUNT(*) FROM hide_migration_v13",
            ver_cb, &rows, &error);
    if (!error) {
        sqlite3_exec(db,
                "SELECT COUNT(*) FROM hide_migration_v13 "
                "WHERE id=1 AND strategy='union-preserve-legacy' "
                "AND source_hidelist_rows>=0 AND source_denylist_rows>=0 "
                "AND source_sulist_rows>=0 AND overlap_rows>=0 "
                "AND migrated_rows>=0 AND malformed_hidelist_rows>=0 "
                "AND sulist_enabled IN (0,1) "
                "AND source_hidelist_rows="
                    "malformed_hidelist_rows+overlap_rows+migrated_rows "
                "AND overlap_rows<=source_denylist_rows",
                ver_cb, &valid_rows, &error);
    }
    return error == nullptr && rows == 1 && valid_rows == 1;
}

static bool hide_migration_committed(sqlite3 *db) {
    // A cancellation can be observed after COMMIT has become durable. Accept
    // only a complete marker in autocommit mode; user_version intentionally
    // remains 12 so older published daemons can reopen the same database.
    if (!sqlite3_get_autocommit(db))
        return false;
    bool exists = false;
    char *error = nullptr;
    const bool complete = hide_migration_marker(db, exists, error);
    if (error)
        sqlite3_free(error);
    return exists && complete;
}

static char *open_and_init_db(sqlite3 *&db) {
    if (!dload_sqlite())
        return db_strdup("Cannot load libsqlite.so");

    int ret = sqlite3_open_v2(MAGISKDB, &db,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (ret) {
        char *error = db_strdup(db ? sqlite3_errmsg(db) : "Cannot open Magisk database");
        if (db) {
            sqlite3_close(db);
            db = nullptr;
        }
        return error;
    }
    int ver = 0;
    int schema_tables = 0;
    bool upgrade = false;
    char *err = nullptr;
    sqlite3_exec(db, "PRAGMA user_version", ver_cb, &ver, &err);
    err_ret(err);
    sqlite3_exec(db,
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            ver_cb, &schema_tables, &err);
    err_ret(err);
    const bool fresh_database = schema_tables == 0;

    bool migration_marker_exists = false;
    bool migration_complete = hide_migration_marker(
            db, migration_marker_exists, err);
    err_ret(err);
    if (migration_marker_exists && !migration_complete)
        return db_strdup("Invalid hide migration marker");

    // Normalize databases produced by the short-lived v13 development build.
    // Its schema is exactly the marker migration below; accepting any other
    // future schema would turn a compatibility repair into an unsafe downgrade.
    if (ver == 13) {
        if (!migration_complete)
            return db_strdup("Version 13 database is missing its hide migration marker");
        sqlite3_exec(db, "PRAGMA user_version=12", nullptr, nullptr, &err);
        err_ret(err);
        ver = 12;
    }
    if (ver > DB_VERSION) {
        // Don't support downgrading database
        sqlite3_close(db);
        db = nullptr;
        return db_strdup("Downgrading database is not supported");
    }

    auto create_policy = [&] {
        sqlite3_exec(db,
                "CREATE TABLE IF NOT EXISTS policies "
                "(uid INT, policy INT, until INT, logging INT, "
                "notification INT, PRIMARY KEY(uid))",
                nullptr, nullptr, &err);
    };
    auto create_settings = [&] {
        sqlite3_exec(db,
                "CREATE TABLE IF NOT EXISTS settings "
                "(key TEXT, value INT, PRIMARY KEY(key))",
                nullptr, nullptr, &err);
    };
    auto create_strings = [&] {
        sqlite3_exec(db,
                "CREATE TABLE IF NOT EXISTS strings "
                "(key TEXT, value TEXT, PRIMARY KEY(key))",
                nullptr, nullptr, &err);
    };
    auto create_denylist = [&] {
        sqlite3_exec(db,
                "CREATE TABLE IF NOT EXISTS denylist "
                "(package_name TEXT, process TEXT, PRIMARY KEY(package_name, process))",
                nullptr, nullptr, &err);
    };

    // Database changelog:
    //
    // 0 - 6: DB stored in app private data. There are no longer any code in the project to
    //        migrate these data, so no need to take any of these versions into consideration.
    // 7 : create table `hidelist` (process TEXT, PRIMARY KEY(process))
    // 8 : add new column (package_name TEXT) to table `hidelist`
    // 9 : rebuild table `hidelist` to change primary key (PRIMARY KEY(package_name, process))
    // 10: remove table `logs`
    // 11: remove table `hidelist` and create table `denylist` (same data structure)
    // 12: rebuild table `policies` to drop column `package_name`
    // marker: union valid legacy `hidelist` rows into canonical `denylist`,
    //         preserve `hidelist`/`sulist`, and record counts/conflict strategy

    if (/* 0, 1, 2, 3, 4, 5, 6 */ ver <= 6) {
        create_policy();
        err_ret(err);
        create_settings();
        err_ret(err);
        create_strings();
        err_ret(err);
        create_denylist();
        err_ret(err);

        // Directly jump to latest
        ver = DB_VERSION;
        upgrade = true;
    }
    if (ver == 7) {
        sqlite3_exec(db,
                "BEGIN TRANSACTION;"
                "ALTER TABLE hidelist RENAME TO hidelist_tmp;"
                "CREATE TABLE IF NOT EXISTS hidelist "
                "(package_name TEXT, process TEXT, PRIMARY KEY(package_name, process));"
                "INSERT INTO hidelist SELECT process as package_name, process FROM hidelist_tmp;"
                "DROP TABLE hidelist_tmp;"
                "COMMIT;",
                nullptr, nullptr, &err);
        err_ret(err);
        // Directly jump to version 9
        ver = 9;
        upgrade = true;
    }
    if (ver == 8) {
        sqlite3_exec(db,
                "BEGIN TRANSACTION;"
                "ALTER TABLE hidelist RENAME TO hidelist_tmp;"
                "CREATE TABLE IF NOT EXISTS hidelist "
                "(package_name TEXT, process TEXT, PRIMARY KEY(package_name, process));"
                "INSERT INTO hidelist SELECT * FROM hidelist_tmp;"
                "DROP TABLE hidelist_tmp;"
                "COMMIT;",
                nullptr, nullptr, &err);
        err_ret(err);
        ver = 9;
        upgrade = true;
    }
    if (ver == 9) {
        sqlite3_exec(db, "DROP TABLE IF EXISTS logs", nullptr, nullptr, &err);
        err_ret(err);
        ver = 10;
        upgrade = true;
    }
    if (ver == 10) {
        err_ret(err);
        create_denylist();
        err_ret(err);
        ver = 11;
        upgrade = true;
    }
    if (ver == 11) {
        sqlite3_exec(db,
                "BEGIN TRANSACTION;"
                "ALTER TABLE policies RENAME TO policies_tmp;"
                "CREATE TABLE IF NOT EXISTS policies "
                "(uid INT, policy INT, until INT, logging INT, "
                "notification INT, PRIMARY KEY(uid));"
                "INSERT INTO policies "
                "SELECT uid, policy, until, logging, notification FROM policies_tmp;"
                "DROP TABLE policies_tmp;"
                "COMMIT;",
                nullptr, nullptr, &err);
        err_ret(err);
        ver = 12;
        upgrade = true;
    }
    if (ver == 12 && !migration_complete) {
        if (upgrade) {
            // Older schemas reach the complete v12 layout through the steps
            // above, but historically user_version was advanced only at the
            // end. Materialize that durable checkpoint so the online backup is
            // both restorable and truthfully identifiable as version 12.
            sqlite3_exec(db, "PRAGMA user_version=12", nullptr, nullptr, &err);
            err_ret(err);
        }
        // A truly new database has no user data to migrate or recover. Existing
        // schemas get one durable pre-change backup before canonical rows or the
        // marker can change; failure to create it blocks reconciliation.
        if (!fresh_database) {
            err = backup_v12_database(db);
            err_ret(err);
        }
        ret = sqlite3_exec(db, HIDE_TABLE_COMPAT_MIGRATION, nullptr, nullptr, &err);
        if (!ret)
            ret = sqlite3_exec(db, HIDE_TABLE_RECONCILE, nullptr, nullptr, &err);
        if (!ret)
            ret = sqlite3_exec(db, "COMMIT", nullptr, nullptr, &err);
        if (ret) {
            if (!err)
                err = db_strdup(sqlite3_errmsg(db));
            // sqlite3_exec stops at the first failing statement. Always close the
            // explicit transaction. A late cancellation can be reported after
            // COMMIT; distinguish that complete state from a real rollback.
            char *rollback_err = nullptr;
            sqlite3_exec(db, "ROLLBACK", nullptr, nullptr, &rollback_err);
            if (rollback_err)
                sqlite3_free(rollback_err);
            if (hide_migration_committed(db)) {
                sqlite3_free(err);
                err = nullptr;
            } else {
                return err;
            }
        }
    }

    if (upgrade) {
        // Set version
        char query[32];
        sprintf(query, "PRAGMA user_version=%d", ver);
        sqlite3_exec(db, query, nullptr, nullptr, &err);
        err_ret(err);
    }
    sqlite3_exec(db, "REPLACE INTO settings (key,value) VALUES('denylist',0);"
                     "CREATE TABLE IF NOT EXISTS hidelist "
                "(package_name TEXT, process TEXT, PRIMARY KEY(package_name, process));"
                     "CREATE TABLE IF NOT EXISTS sulist "
                "(package_name TEXT, process TEXT, PRIMARY KEY(package_name, process));", nullptr, nullptr, &err);
    err_ret(err);
    return nullptr;
}

static char *ensure_db_open() {
    if (mDB)
        return nullptr;

    char *err = open_and_init_db(mDB);
    if (err && mDB) {
        // Initialization and migrations fail closed. Never interpret a schema,
        // migration, or future-version error as permission to delete user data.
        sqlite3_close(mDB);
        mDB = nullptr;
    }
    return err;
}

char *db_exec(const char *sql) {
    char *err = ensure_db_open();
    err_ret(err);
    if (mDB) {
        sqlite3_exec(mDB, sql, nullptr, nullptr, &err);
        return err;
    }
    return nullptr;
}

static int sqlite_db_row_callback(void *cb, int col_num, char **data, char **col_name) {
    auto &func = *static_cast<const db_row_cb*>(cb);
    db_row row;
    for (int i = 0; i < col_num; ++i) {
        // sqlite3_exec represents SQL NULL with a null pointer. Constructing a
        // string_view from it is undefined behavior and let `magisk --sqlite`
        // crash the daemon on otherwise valid queries such as SELECT NULL.
        row[col_name[i]] = data[i] ? data[i] : "";
    }
    return func(row) ? 0 : 1;
}

char *db_exec(const char *sql, const db_row_cb &fn) {
    char *err = ensure_db_open();
    err_ret(err);
    if (mDB) {
        sqlite3_exec(mDB, sql, sqlite_db_row_callback, (void *) &fn, &err);
        return err;
    }
    return nullptr;
}

int get_db_settings(db_settings &cfg, int key) {
    char *err = nullptr;
    auto settings_cb = [&](db_row &row) -> bool {
        cfg[row["key"]] = parse_int(row["value"]);
        DBLOGV("query %s=[%s]\n", row["key"].data(), row["value"].data());
        return true;
    };
    if (key >= 0) {
        char query[128];
        ssprintf(query, sizeof(query), "SELECT * FROM settings WHERE key='%s'", DB_SETTING_KEYS[key]);
        err = db_exec(query, settings_cb);
    } else {
        err = db_exec("SELECT * FROM settings", settings_cb);
    }
    db_err_cmd(err, return 1);
    return 0;
}

int get_db_strings(db_strings &str, int key) {
    char *err = nullptr;
    auto string_cb = [&](db_row &row) -> bool {
        str[row["key"]] = row["value"];
        DBLOGV("query %s=[%s]\n", row["key"].data(), row["value"].data());
        return true;
    };
    if (key >= 0) {
        char query[128];
        ssprintf(query, sizeof(query), "SELECT * FROM strings WHERE key='%s'", DB_STRING_KEYS[key]);
        err = db_exec(query, string_cb);
    } else {
        err = db_exec("SELECT * FROM strings", string_cb);
    }
    db_err_cmd(err, return 1);
    return 0;
}

void rm_db_strings(int key) {
    char *err;
    char query[128];
    ssprintf(query, sizeof(query), "DELETE FROM strings WHERE key == '%s'", DB_STRING_KEYS[key]);
    err = db_exec(query);
    db_err_cmd(err, return);
}

void exec_sql(int client) {
    run_finally f([=]{ close(client); });
    string sql = read_string(client);
    char *err = db_exec(sql.data(), [client](db_row &row) -> bool {
        string out;
        bool first = true;
        for (auto it : row) {
            if (first) first = false;
            else out += '|';
            out += it.first;
            out += '=';
            out += it.second;
        }
        write_string(client, out);
        return true;
    });
    write_int(client, 0);
    db_err_cmd(err, return; );
}

bool db_err(char *e) {
    if (e) {
        LOGE("sqlite3_exec: %s\n", e);
        if (sqlite_error_allocator)
            sqlite3_free(e);
        else
            free(e);
        return true;
    }
    return false;
}
