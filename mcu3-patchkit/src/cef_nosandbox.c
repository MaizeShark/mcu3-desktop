// LD_PRELOAD shim: start QtCar's Chromium (CEF) without its sandbox, optionally with logging.
//
// The renderer sandbox (namespaces, seccomp, the setuid chrome-sandbox helper) can't work in the
// chroot. CEF skips it when CefSettings.no_sandbox is set; QtCar never sets it. The old byte patch
// "gui-cef-nosandbox" tried to, but patched a copy of ChromiumManager::initializeCef in
// libQtCarGUI that's never used (QtCar's executable has its own) and wrote to a wrong register.
// This wraps cef_initialize() instead: adjust the settings, call the real one. QtCar calls it
// through its PLT, so the preloaded symbol wins.
//
// cef_settings_t of this CEF (3.3683, Chrome 73; size 360), offsets checked by dumping QtCar's
// settings: +0 size, +8 no_sandbox, +16 browser_subprocess_path, +80 cache_path,
// +136 user_agent, +184 locale, +208 log_file, +232 log_severity (99 = disabled),
// +316 remote_debugging_port.
//
// Environment (all optional):
//   CEF_LOG_SEVERITY=verbose|info|warning|error   CEF's own log (QtCar has it off)
//   CEF_LOG_FILE=/tmp/cef.log                     where it goes (default: stderr + debug.log)
//   CEF_REMOTE_DEBUGGING_PORT=9222                DevTools on that port
//   CEF_EXTRA_ARGS="--switch --other=x"           Chromium switches appended to CEF's command
//                                                 line (QtCar passes its own argv to CEF)
//   CEF_SHIM_DEBUG=1                              log what this shim does to stderr
//
// Build (must run against the firmware's glibc 2.22, so no newer symbol versions):
//   gcc -shared -fPIC -O2 -o cef_nosandbox.so cef_nosandbox.c
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// glibc >= 2.34 exports dlsym with a new version the firmware's glibc doesn't have
__asm__(".symver dlsym,dlsym@GLIBC_2.2.5");

#define SETTINGS_SIZE 360
#define OFF_NO_SANDBOX 8
#define OFF_LOG_FILE 208
#define OFF_LOG_SEVERITY 232
#define OFF_REMOTE_DEBUGGING_PORT 316

typedef struct {
    uint16_t *str;
    size_t length;
    void (*dtor)(uint16_t *);
} cef_string_t;

typedef struct {
    int argc;
    char **argv;
} cef_main_args_t;

typedef int (*cef_initialize_fn)(const void *args, const void *settings, void *app, void *sandbox_info);

// not atoi/strtol: with a new glibc those become __isoc23_strtol@GLIBC_2.38
static int to_int(const char *s)
{
    int n = 0;
    while (*s >= '0' && *s <= '9')
        n = n * 10 + (*s++ - '0');
    return n;
}

static int severity(const char *s)
{
    static const struct { const char *name; int value; } map[] = {
        {"verbose", 1}, {"debug", 1}, {"info", 2}, {"warning", 3}, {"error", 4}, {"fatal", 5}, {"disable", 99}};
    for (size_t i = 0; i < sizeof map / sizeof map[0]; i++)
        if (strcmp(s, map[i].name) == 0)
            return map[i].value;
    return to_int(s);
}

int cef_initialize(const void *args, const void *settings, void *app, void *sandbox_info)
{
    static cef_initialize_fn real;
    static uint16_t log_file[512];
    int debug = getenv("CEF_SHIM_DEBUG") != NULL;
    if (!real)
        real = (cef_initialize_fn)dlsym(RTLD_NEXT, "cef_initialize");
    if (!real) {
        fprintf(stderr, "cef_nosandbox: real cef_initialize not found\n");
        return 0;
    }
    unsigned char *s = (unsigned char *)settings;
    size_t size = s ? *(size_t *)s : 0;
    if (size != SETTINGS_SIZE) {
        fprintf(stderr, "cef_nosandbox: unexpected cef_settings_t size %zu (want %d), not touching it\n",
                size, SETTINGS_SIZE);
        return real(args, settings, app, sandbox_info);
    }
    *(int *)(s + OFF_NO_SANDBOX) = 1;
    const char *v;
    if ((v = getenv("CEF_LOG_SEVERITY")) && *v)
        *(int *)(s + OFF_LOG_SEVERITY) = severity(v);
    if ((v = getenv("CEF_LOG_FILE")) && *v) {
        size_t n = strlen(v);
        if (n >= sizeof log_file / sizeof log_file[0])
            n = sizeof log_file / sizeof log_file[0] - 1;
        for (size_t i = 0; i < n; i++)
            log_file[i] = (unsigned char)v[i];
        cef_string_t *f = (cef_string_t *)(s + OFF_LOG_FILE);
        f->str = log_file;         // static buffer: no destructor; the old value (empty) is dropped
        f->length = n;
        f->dtor = NULL;
    }
    if ((v = getenv("CEF_REMOTE_DEBUGGING_PORT")) && *v)
        *(int *)(s + OFF_REMOTE_DEBUGGING_PORT) = to_int(v);
    if (debug)
        fprintf(stderr, "cef_nosandbox: no_sandbox=1 log_severity=%d remote_debugging_port=%d\n",
                *(int *)(s + OFF_LOG_SEVERITY), *(int *)(s + OFF_REMOTE_DEBUGGING_PORT));
    // extra Chromium switches: a copy of QtCar's argv with CEF_EXTRA_ARGS appended
    static char extra[1024];
    static char *argv[128];
    static cef_main_args_t new_args;
    const cef_main_args_t *margs = args;
    if (margs && (v = getenv("CEF_EXTRA_ARGS")) && *v) {
        int n = 0;
        for (int i = 0; i < margs->argc && n < 100; i++)
            argv[n++] = margs->argv[i];
        strncpy(extra, v, sizeof extra - 1);
        for (char *p = extra; *p && n < 127;) {
            while (*p == ' ')
                *p++ = 0;
            if (!*p)
                break;
            argv[n++] = p;
            while (*p && *p != ' ')
                p++;
        }
        argv[n] = NULL;
        new_args.argc = n;
        new_args.argv = argv;
        args = &new_args;
        if (debug)
            fprintf(stderr, "cef_nosandbox: extra switches: %s\n", v);
    }
    int r = real(args, settings, app, sandbox_info);
    if (debug)
        fprintf(stderr, "cef_nosandbox: cef_initialize -> %d\n", r);
    return r;
}
