#include <sys/types.h>

#include "api.hpp"

class TestModule final : public zygisk::ModuleBase {};

REGISTER_ZYGISK_MODULE(TestModule)
