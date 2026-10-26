/**
 * getopt_compat.h – POSIX getopt/getopt_long shim for MSVC on Windows.
 *
 * On non-Windows platforms this simply includes <getopt.h> and <unistd.h>.
 * On Windows/MSVC it provides a minimal but complete implementation of
 * getopt_long and the few unistd symbols used by the benchmarks
 * (write, STDOUT_FILENO, _exit).
 */
#pragma once

#ifdef _WIN32

    /* ── unistd replacements ─────────────────────────────────────────────── */
    #include <io.h>      /* _write  */
    #include <process.h> /* _exit, _getpid */
    #include <stdlib.h>

    #ifndef STDOUT_FILENO
        #define STDOUT_FILENO 1
    #endif
    #ifndef STDERR_FILENO
        #define STDERR_FILENO 2
    #endif

/* POSIX write() → MSVC _write()  (inline function avoids macro poisoning member functions like std::ostream::write) */
static inline int write(int fd, const void* buf, unsigned count) {
    return _write(fd, buf, count);
}

    /* POSIX getpid() → MSVC _getpid() */
    #ifndef getpid
        #define getpid _getpid
    #endif

/* POSIX setenv() → MSVC _putenv_s() */
static inline int setenv(const char* name, const char* value, int overwrite) {
    if (!overwrite) {
        size_t sz = 0;
        if (getenv_s(&sz, NULL, 0, name) == 0 && sz > 0)
            return 0; /* variable already exists, don't overwrite */
    }
    return _putenv_s(name, value);
}

    /* ── getopt / getopt_long ────────────────────────────────────────────── */
    #include <string.h>
    #include <stdio.h>

    #ifdef __cplusplus
extern "C" {
    #endif

inline char* optarg = NULL;
inline int optind = 1;
inline int opterr = 1;
inline int optopt = 0;

struct option {
    const char* name;
    int has_arg;
    int* flag;
    int val;
};

    #define no_argument 0
    #define required_argument 1
    #define optional_argument 2

inline int getopt_long(int argc, char* const argv[], const char* optstring, const struct option* longopts, int* longindex) {
    static int pos = 1; /* position inside grouped short opts */
    if (optind <= 1)
        pos = 1; /* reset when caller resets optind */

    optarg = NULL;

    while (optind < argc) {
        const char* arg = argv[optind];

        /* -- stops processing */
        if (strcmp(arg, "--") == 0) {
            ++optind;
            return -1;
        }

        /* Long option */
        if (arg[0] == '-' && arg[1] == '-') {
            const char* name = arg + 2;
            const char* eq = strchr(name, '=');
            size_t namelen = eq ? (size_t)(eq - name) : strlen(name);

            for (int i = 0; longopts && longopts[i].name; ++i) {
                if (strncmp(longopts[i].name, name, namelen) == 0 && strlen(longopts[i].name) == namelen) {
                    if (longindex)
                        *longindex = i;
                    ++optind;
                    if (longopts[i].has_arg == required_argument || longopts[i].has_arg == optional_argument) {
                        if (eq)
                            optarg = (char*)(eq + 1);
                        else if (longopts[i].has_arg == required_argument && optind < argc)
                            optarg = argv[optind++];
                    }
                    if (longopts[i].flag) {
                        *longopts[i].flag = longopts[i].val;
                        return 0;
                    }
                    return longopts[i].val;
                }
            }
            if (opterr)
                fprintf(stderr, "Unknown option: %s\n", arg);
            ++optind;
            return '?';
        }

        /* Short option */
        if (arg[0] == '-' && arg[1] != '\0') {
            char c = arg[pos];
            const char* spec = strchr(optstring, c);
            if (!spec) {
                optopt = c;
                if (opterr)
                    fprintf(stderr, "Unknown option: -%c\n", c);
                if (!arg[++pos]) {
                    ++optind;
                    pos = 1;
                }
                return '?';
            }
            if (spec[1] == ':') { /* requires argument */
                if (arg[pos + 1]) {
                    optarg = (char*)(arg + pos + 1);
                } else if (optind + 1 < argc) {
                    optarg = argv[++optind];
                } else {
                    if (opterr)
                        fprintf(stderr, "Option -%c requires an argument\n", c);
                    ++optind;
                    pos = 1;
                    return '?';
                }
                ++optind;
                pos = 1;
                return c;
            }
            /* no argument */
            if (!arg[++pos]) {
                ++optind;
                pos = 1;
            }
            return c;
        }

        /* Not an option */
        return -1;
    }
    return -1;
}

    #ifdef __cplusplus
}
    #endif

#else /* !_WIN32 ── POSIX ─────────────────────────────────────────────── */

    #include <getopt.h>
    #include <unistd.h>

#endif /* _WIN32 */
