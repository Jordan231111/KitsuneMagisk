#pragma once
#include <sys/wait.h>
#include <signal.h>

int bind_mount_(const char *from, const char *to);
int tmpfs_mount(const char *from, const char *to);
bool selinux_enabled();
