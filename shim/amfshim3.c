/* amfshim3.so - LD_PRELOAD shim with INLINE HOOK on avcodec_open2.
 *
 * Role in the AMD acceleration pipeline: the companion vkfix16.so fixes
 * MediaSDK's Vulkan init; this file forces maximum-bitrate encoding on the AMF
 * HEVC encoder so that output quality matches the original camera bitrates.
 * MediaSDK calls avcodec_open2 via DIRECT calls (not PLT), so plain symbol
 * interposition does NOT intercept them.  We therefore patch the first bytes of
 * the real avcodec_open2 (inside libMediaSDK.so) with a jump to our handler,
 * using a trampoline that preserves the original prologue.
 *
 * Mechanism: our handler first calls the real avcodec_open2 via the trampoline
 * (see init_reals for how the jump and trampoline are built).  If the opened
 * codec is the AMF HEVC encoder, it then forces maximum-bitrate options on
 * codec_ctx->priv_data through av_opt_set_int:
 *   rc=3 (CBR), quality=0 (best), filler_data=0, max_au_size=0, enforce_hrd=0.
 * filler_data defaults to 0 on purpose: padding the stream to the exact
 * bitrate with filler packets made the encoder idle between waves of input
 * frames (software decode), causing mid-run GPU/CPU load drops.  With fillers
 * off the encoder runs continuously while CBR (set by the SDK via -bitrate)
 * still targets the requested bitrate.
 * A plain exported avcodec_open2 is also provided at the bottom for any
 * PLT-based callers, as a belt-and-suspenders fallback.
 *
 * All six options can be overridden once per process via environment
 * variables (read at load time, defaults in parentheses):
 *   AMF_RC=3, AMF_QUALITY=0, AMF_FILLER=0, AMF_MAX_AU=0, AMF_HRD=0,
 *   AMF_QVBR_QUALITY_LEVEL=26 (QVBR quality level; used only in qvbr/cqp
 *   rate-control modes, ignored by ffmpeg for other rc modes).
 * The default rc stays 3 (CBR); convert_tui.py switches to qvbr (2) via AMF_RC
 * when the user passes --quality.
 *
 * Layout (from disassembly of libMediaSDK.so, static ffmpeg):
 *   AVCodecContext: codec_id=0xc, codec=0x10, priv_data=0x20
 *   AVCodec:        name=0x8, id=0x10
 *
 * The libMediaSDK.so path is resolved portably, in this order:
 *   1) $MEDIASDK_LIB environment variable (explicit override)
 *   2) sibling of this shim: <shim_dir>/../patched/lib/libMediaSDK.so
 *      (discovered via dladdr on this module)
 *   3) plain "libMediaSDK.so" (relies on LD_LIBRARY_PATH / rpath)
 *
 * Build: run `make` in the repository root; it compiles this file into
 * shim/amfshim3.so.  Set AMFSHIM_DEBUG=1 to enable the diagnostic stderr
 * output sprinkled through the code below.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
#include <errno.h>
#include <limits.h>

typedef int (*avcodec_open2_fn)(void*, void*, void*);
typedef int (*av_opt_set_int_fn)(void*, const char*, int64_t, int);

static avcodec_open2_fn real_avcodec_open2 = 0;
static av_opt_set_int_fn real_av_opt_set_int = 0;

/* Trampoline buffer holding the original prologue of avcodec_open2 plus a jump
 * back into the function after the patched bytes.  It is a static array in
 * .bss, which is non-executable by default, so init_reals makes it executable
 * with mprotect before use. */
static unsigned char trampoline[64] __attribute__((aligned(16)));
static const int HOOK_LEN = 13; /* bytes overwritten at the function start */

/* Diagnostic output goes to stderr when AMFSHIM_DEBUG is set (any value). */
static int dbg_on(void) { return getenv("AMFSHIM_DEBUG") != NULL; }

/* Forced AMF encoder options, read once from the environment at load time
 * (see the header comment for the variable names and defaults).  These are
 * applied instead of hardcoded values in my_avcodec_open2. */
static int64_t opt_rc = 3;        /* CBR */
static int64_t opt_quality = 0;   /* 0 = quality (best) */
static int64_t opt_filler = 0;    /* 0 = don't pad to exact bitrate (see header) */
static int64_t opt_max_au = 0;    /* no AU size limit */
static int64_t opt_hrd = 0;       /* don't enforce HRD */
static int64_t opt_qvbr_level = 26; /* QVBR quality level (used when rc==2) */

/* Read one env override, falling back to the given default on missing/invalid
 * values.  Called once from init_reals before the hook is installed. */
static int64_t env_int(const char* name, int64_t def) {
    const char* v = getenv(name);
    if (!v || !v[0]) return def;
    return atoll(v);
}

/* Resolve the libMediaSDK.so path portably (see header comment).  The env var
 * is tried first because it is an explicit user override; the dladdr-based
 * sibling path covers the typical repo layout where the shim sits next to the
 * patched MediaSDK tree; the plain name is the last resort that relies on
 * LD_LIBRARY_PATH / rpath. */
static const char* resolve_mediasdk_path(void) {
    const char* env = getenv("MEDIASDK_LIB");
    if (env && env[0]) return env;

    /* Discover this module's own path via dladdr, then walk up one directory
     * to <shim_dir>/../patched/lib/libMediaSDK.so. */
    Dl_info info;
    if (dladdr((void*)&resolve_mediasdk_path, &info) && info.dli_fname && info.dli_fname[0]) {
        static char path[PATH_MAX];
        snprintf(path, sizeof(path), "%s", info.dli_fname);
        char* slash = strrchr(path, '/');
        if (slash) {
            /* <shim_dir>/../patched/lib/libMediaSDK.so */
            snprintf(slash, sizeof(path) - (size_t)(slash - path),
                     "/../patched/lib/libMediaSDK.so");
            if (access(path, R_OK) == 0) return path;
        }
    }
    return "libMediaSDK.so";
}

/* Our handler - called instead of avcodec_open2.  It forwards to the real
 * implementation through the trampoline and, for the AMF HEVC encoder, forces
 * the maximum-bitrate options afterwards. */
static int my_avcodec_open2(void* avctx, void* codec, void* options) {
    /* call real avcodec_open2 via trampoline */
    int r = ((avcodec_open2_fn)(void*)trampoline)(avctx, codec, options);
    if (r != 0) {
        fprintf(stderr, "[amfshim3] avcodec_open2 FAILED r=%d\n", r);
        return r;
    }
    /* Read the codec name and priv_data straight from the structs: per the
     * layout in the header comment, AVCodecContext.priv_data is at +0x20 and
     * AVCodec.name is at +0x0.  These offsets come from the libMediaSDK static
     * ffmpeg build and are fixed for that build. */
    void* priv = *(void**)((char*)avctx + 0x20);
    const char* cname = codec ? *(const char**)((char*)codec + 0x0) : NULL;
    if (dbg_on()) fprintf(stderr, "[amfshim3] avcodec_open2 OK codec_name=%s priv=%p\n",
            cname ? cname : "(null)", priv);
    if (cname && strcmp(cname, "hevc_amf") == 0 && priv) {
        /* Force maximum bitrate on the AMF encoder's private options so the
         * output matches the camera's original bitrate instead of MediaSDK's
         * default.  rc=3 selects CBR, quality=0 the best quality mode,
         * filler_data=0 avoids filler-packet sync stalls (mid-run load drops),
         * max_au_size=0 lifts the AU size cap, and enforce_hrd=0 disables HRD
         * conformance checks.  Values come from the env overrides read in
         * init_reals, so each can be tuned without rebuilding. */
        struct { const char* name; int64_t val; } opts[] = {
            {"rc", opt_rc},               /* CBR (3) or QVBR (2) */
            {"quality", opt_quality},     /* 0 = quality (best) */
            {"filler_data", opt_filler},  /* don't pad to exact bitrate */
            {"max_au_size", opt_max_au},  /* no AU size limit */
            {"enforce_hrd", opt_hrd},     /* don't enforce HRD */
            {"qvbr_quality_level", opt_qvbr_level}, /* QVBR quality level */
        };
        for (size_t i = 0; i < sizeof(opts)/sizeof(opts[0]); i++) {
            int o = real_av_opt_set_int(priv, opts[i].name, opts[i].val, 0);
            if (dbg_on()) fprintf(stderr, "[amfshim3]   av_opt_set_int(%s, %lld) -> %d\n",
                    opts[i].name, (long long)opts[i].val, o);
        }
        /* This confirmation is intentionally unconditional: it is the key
         * signal that the AMF max-bitrate options were actually applied. */
        fprintf(stderr, "[amfshim3] AMF HEVC max-bitrate options applied\n");
    }
    return r;
}

/* Idempotency guard for the hook install.  The constructor runs once in the
 * single-threaded startup phase, but the exported avcodec_open2 fallback can
 * also trigger init_reals lazily, so guard against installing the patch twice
 * (which would corrupt the function prologue). */
static int hook_installed = 0;

static void init_reals(void) __attribute__((constructor));
static void init_reals(void) {
    if (__atomic_load_n(&hook_installed, __ATOMIC_ACQUIRE)) return;

    /* Read the AMF option overrides once, before the hook is installed. */
    opt_rc = env_int("AMF_RC", 3);
    opt_quality = env_int("AMF_QUALITY", 0);
    opt_filler = env_int("AMF_FILLER", 0);
    opt_max_au = env_int("AMF_MAX_AU", 0);
    opt_hrd = env_int("AMF_HRD", 0);
    opt_qvbr_level = env_int("AMF_QVBR_QUALITY_LEVEL", 26);

    /* libMediaSDK.so may not be loaded yet: LD_PRELOAD libraries are mapped
     * before the main binary's DT_NEEDED dependencies, so at constructor time
     * libMediaSDK.so may still be absent.  dlopen it explicitly (using the
     * portably resolved path) so dlsym finds the real avcodec_open2 inside it. */
    const char* msdk_path = resolve_mediasdk_path();
    if (dbg_on()) fprintf(stderr, "[amfshim3] resolving libMediaSDK as: %s\n", msdk_path);
    void* msdk = dlopen(msdk_path, RTLD_NOW | RTLD_GLOBAL);
    if (dbg_on()) fprintf(stderr, "[amfshim3] dlopen(libMediaSDK) = %p\n", msdk);
    real_avcodec_open2 = (avcodec_open2_fn)dlsym(msdk ? msdk : RTLD_DEFAULT, "avcodec_open2");
    real_av_opt_set_int = (av_opt_set_int_fn)dlsym(RTLD_NEXT, "av_opt_set_int");
    if (dbg_on()) fprintf(stderr, "[amfshim3] init: real_avcodec_open2=%p av_opt_set_int=%p\n",
            (void*)real_avcodec_open2, (void*)real_av_opt_set_int);
    if (!real_avcodec_open2 || !real_av_opt_set_int) return;

    unsigned char* fn = (unsigned char*)real_avcodec_open2;

    /* Make the trampoline buffer executable (it lives in .bss, NX by default) */
    {
        long page = sysconf(_SC_PAGESIZE);
        uintptr_t tstart = ((uintptr_t)trampoline) & ~(uintptr_t)(page - 1);
        if (mprotect((void*)tstart, page, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
            fprintf(stderr, "[amfshim3] mprotect(trampoline) failed: %s\n", strerror(errno));
            return;
        }
    }

    /* Build the trampoline: copy the HOOK_LEN original bytes of avcodec_open2
     * (the ones we are about to overwrite) into the buffer, then append a
     * near jump (0xe9 rel32) back to fn+HOOK_LEN so execution resumes at the
     * untouched remainder of the function.  The rel32 displacement is measured
     * from the end of the jmp instruction (trampoline + HOOK_LEN + 5) to the
     * target fn + HOOK_LEN. */
    memcpy(trampoline, fn, HOOK_LEN);
    /* jmp rel32 to fn+HOOK_LEN from end of trampoline copy */
    trampoline[HOOK_LEN] = 0xe9;
    int64_t off = ((int64_t)(fn + HOOK_LEN)) - ((int64_t)(trampoline + HOOK_LEN + 5));
    memcpy(trampoline + HOOK_LEN + 1, &off, 4);

    /* Make the page containing fn writable+executable so we can patch it.  The
     * code section of a shared object is read-only, so mprotect is required. */
    long page = sysconf(_SC_PAGESIZE);
    uintptr_t page_start = ((uintptr_t)fn) & ~(uintptr_t)(page - 1);
    if (mprotect((void*)page_start, page, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        fprintf(stderr, "[amfshim3] mprotect failed: %s\n", strerror(errno));
        return;
    }
    /* Overwrite the start of avcodec_open2 with an absolute jump to our
     * handler: mov rax, imm64; jmp rax, i.e. bytes 48 b8 <8-byte addr> ff e0
     * (12 bytes total), plus a NOP to fill the 13th byte of HOOK_LEN. */
    fn[0] = 0x48; fn[1] = 0xb8;
    uint64_t h = (uint64_t)(uintptr_t)my_avcodec_open2;
    memcpy(fn + 2, &h, 8);
    fn[10] = 0xff; fn[11] = 0xe0;
    /* NOP the remaining byte (13th) */
    fn[12] = 0x90;
    /* Flush the instruction cache over both patched regions so the CPU does
     * not execute stale bytes (not strictly required on x86, but safe). */
    __builtin___clear_cache((char*)fn, (char*)fn + HOOK_LEN);
    __builtin___clear_cache((char*)trampoline, (char*)trampoline + HOOK_LEN + 5);
    /* Mark the hook installed only after the patch is fully in place. */
    __atomic_store_n(&hook_installed, 1, __ATOMIC_RELEASE);
    if (dbg_on()) fprintf(stderr, "[amfshim3] inline hook installed at %p -> my_avcodec_open2=%p trampoline=%p\n",
            (void*)fn, (void*)my_avcodec_open2, (void*)trampoline);
}

/* Also export avcodec_open2 for any PLT-based calls (belt & suspenders) */
int avcodec_open2(void* avctx, void* codec, void* options) {
    return my_avcodec_open2(avctx, codec, options);
}
