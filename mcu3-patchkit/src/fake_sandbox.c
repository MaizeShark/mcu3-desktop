// Reconstructed from the binary installed at /usr/tesla/UI/bin/chrome-sandbox.
// Dummy SUID sandbox for CEF: just exec the command it is asked to run, without sandboxing.
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc > 1)
        execv(argv[1], &argv[1]);
    return 0;
}
