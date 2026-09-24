#include <stdlib.h>
#include <fcntl.h>
#include <stdio.h>

__attribute__((constructor))
void init_icu() {
    int fd = open("/usr/lib/icudtl.dat", O_RDONLY);
    if (fd >= 0) {
        char buf[32];
        snprintf(buf, sizeof(buf), "%d", fd);
        setenv("ICU_DATA_FD", buf, 1);
        fprintf(stderr, "ICU preload: opened icudtl.dat as fd %d\n", fd);
    }
}
