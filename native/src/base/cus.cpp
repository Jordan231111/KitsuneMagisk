#include <sys/mount.h>
#include <unistd.h>

#include <base.hpp>

#define VLOGDG(tag, from, to) LOGD("%-8s: %s <- %s\n", tag, to, from)

int bind_mount_(const char *from, const char *to) {
    int ret = xmount(from, to, nullptr, MS_BIND, nullptr);
    if (ret == 0)
        VLOGDG("bind_mnt", from, to);
    return ret;
}

int tmpfs_mount(const char *from, const char *to){
    int ret = xmount(from, to, "tmpfs", 0, "mode=755");
    if (ret == 0)
        VLOGDG("mnt_tmp", "tmpfs", to);
    return ret;
}

bool selinux_enabled() {
    return access("/sys/fs/selinux/enforce", F_OK) == 0;
}
