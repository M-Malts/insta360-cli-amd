/* vkfix16.so - LD_PRELOAD shim fixing MediaSDK Vulkan init on RADV (v2, AMF-aware).
 *
 * Role in the AMD acceleration pipeline: this shim is preloaded into the
 * MediaSDK process and interposes dlopen/dlsym (see the wrappers at the bottom
 * of this file).  Every vkGetInstanceProcAddr / vkCreateDevice resolved through
 * dlsym is redirected to our hooks below, which repair two broken Vulkan
 * interactions before the real functions ever run.  The companion amfshim3.so
 * handles the libMediaSDK avcodec_open2 path; this file only deals with Vulkan.
 *
 * Why interposition is needed: libMediaSDK stores its Vulkan entry points in
 * plain global (BSS) function-pointer variables that are populated at runtime
 * via vkGetInstanceProcAddr.  Because those pointers live in libMediaSDK's own
 * data segment, LD_PRELOAD interposition of the vk* functions themselves does
 * not affect them; the only reliable hook point is the vkGetInstanceProcAddr
 * call that fills them.  Intercepting it (and vkCreateDevice) through dlsym is
 * therefore the mechanism used here.
 *
 * Fixes:
 *  1) vkGetInstanceProcAddr symbol interposition: our dlsym wrapper returns
 *     my_vkGetInstanceProcAddr for that name, so libMediaSDK's BSS vk* pointers
 *     end up pointing at our fixed implementations.
 *  2) VK_ERROR_NOT_PERMITTED from vkCreateDevice (MediaSDK blend path): RADV
 *     rejects devices that request VK_EXT_global_priority.  We strip the
 *     global-priority pNext structures from BOTH VkDeviceCreateInfo and each
 *     VkDeviceQueueCreateInfo, and drop VK_EXT_global_priority from the
 *     enabled-extension list, before forwarding to the real vkCreateDevice.
 *  3) AMF vkCreateDevice: pass through unchanged (no pNext stripping), so AMF's
 *     own pNext chain is preserved for its encoder device.
 *
 * Only the MediaSDK blend path requests VK_EXT_global_priority, so its presence
 * in pCreateInfo is what tells the two paths apart (see my_vkCreateDevice).
 *
 * This file contains no machine-specific absolute paths and is portable.
 *
 * Build: run `make` in the repository root; it compiles this file into
 * vkshim/vkfix16.so.  Set VKFIX_DEBUG=1 to enable the diagnostic stderr output
 * sprinkled through the code below.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

/* Pointers to the real libc/libvulkan functions, resolved once in the
 * constructor below.  We must go through these rather than calling the symbols
 * directly, otherwise our own interposition wrappers would recurse. */
static void* (*real_dlopen)(const char*, int) = 0;
static void* (*real_dlsym)(void*, const char*) = 0;
static void* vk_handle = 0;
static void* (*real_vkGetInstanceProcAddr)(void*, const char*) = 0;
static int (*real_vkCreateDevice)(void*, const void*, const void*, void**) = 0;

/* Extension whose presence marks the MediaSDK blend path (see my_vkCreateDevice). */
static const char* GP_EXT = "VK_EXT_global_priority";

/* Diagnostic output goes to stderr when VKFIX_DEBUG is set (any value). */
static int dbg_on(void) { return getenv("VKFIX_DEBUG") != NULL; }

/* Hook replacing vkCreateDevice.  We read the fixed field offsets of
 * VkDeviceCreateInfo (from the Vulkan 1.0 ABI) directly from the caller's
 * structure:
 *   sType/pNext at +0/+8, queueCreateInfoCount at +16, pQueueCreateInfos at +24,
 *   enabledExtensionCount at +48, ppEnabledExtensionNames at +56.
 * Each VkDeviceQueueCreateInfo is 40 bytes with its own pNext at +8.
 * These offsets are ABI-stable and match libMediaSDK's build; they must not be
 * changed, only documented here. */
static int my_vkCreateDevice(void* phys, const void* pCreateInfo, const void* pAlloc, void** pDev) {
    if (!pCreateInfo) {
        return real_vkCreateDevice(phys, pCreateInfo, pAlloc, pDev);
    }
    const uint8_t* ci = (const uint8_t*)pCreateInfo;
    uint32_t extCount = *(const uint32_t*)(ci+48);
    const char* const* pExts = *(const char* const* const*)(ci+56);
    int has_gp = 0;
    for (uint32_t i=0;i<extCount;i++) {
        if (pExts && pExts[i] && strcmp(pExts[i], GP_EXT) == 0) { has_gp = 1; break; }
    }
    if (!has_gp) {
        /* No VK_EXT_global_priority: this is not the MediaSDK blend path (it is
         * e.g. the AMF encoder device).  Forward pCreateInfo unchanged so AMF's
         * own pNext chain reaches the driver intact. */
        if (dbg_on()) fprintf(stderr, "[vkfix16] vkCreateDevice: no VK_EXT_global_priority (extCount=%u), pass-through\n", extCount);
        int r = real_vkCreateDevice(phys, pCreateInfo, pAlloc, pDev);
        if (r == 0 && pDev && *pDev) {
            void* magic = *(void**)(*pDev);
            if (dbg_on()) fprintf(stderr, "[vkfix16] vkCreateDevice OK device=%p magic=%p\n", *pDev, magic);
            if (magic && dbg_on()) {
                fprintf(stderr, "[vkfix16]   magic[0]=0x%lx dispatch[3]=%p dispatch[7]=%p\n",
                    *(unsigned long*)magic, *(void**)((char*)magic+0x18), *(void**)((char*)magic+0x38));
            }
        }
        return r;
    }
    /* MediaSDK blend path: strip global priority before forwarding.  We cannot
     * modify the caller's structure (it may be read-only and is reused), so we
     * build a sanitized copy in fixed-size stack buffers.  The copy is 128
     * bytes, queue entries are 40 bytes each, and the extension list is capped
     * at 32 entries - all sized for the MediaSDK usage this shim targets. */
    uint8_t ci_copy[128];
    uint8_t q_copy[4][64];
    const char* new_exts[32];
    uint32_t new_ext_count = 0;
    memcpy(ci_copy, pCreateInfo, 128);
    /* 1. Drop the pNext chain of VkDeviceCreateInfo (offset +8); this removes
     *    the VkPhysicalDeviceGlobalPriorityCreateInfoEXT it carried. */
    *(const void**)(ci_copy+8) = NULL;
    /* 2. Copy each VkDeviceQueueCreateInfo and strip its pNext (offset +8),
     *    which may also carry a global-priority structure. */
    uint32_t qfCount = *(const uint32_t*)(ci_copy+20);
    const uint8_t* pQf = *(const uint8_t* const*)(ci_copy+24);
    if (qfCount > 4) qfCount = 4;
    for (uint32_t i=0;i<qfCount;i++) {
        memcpy(q_copy[i], pQf + i*40, 40);
        *(const void**)(q_copy[i]+8) = NULL; /* strip queue pNext */
    }
    *(const void**)(ci_copy+24) = q_copy;
    /* 3. Drop VK_EXT_global_priority from the enabled-extension list so the
     *    driver no longer sees the extension that triggers VK_ERROR_NOT_PERMITTED. */
    for (uint32_t i=0;i<extCount && i<32;i++) {
        if (pExts && pExts[i] && strcmp(pExts[i], GP_EXT) == 0) continue;
        new_exts[new_ext_count++] = pExts[i];
    }
    *(uint32_t*)(ci_copy+48) = new_ext_count;
    *(const char* const**)(ci_copy+56) = new_exts;
    if (dbg_on()) fprintf(stderr, "[vkfix16] vkCreateDevice: qfCount=%u extCount %u->%u, stripped global priority\n", qfCount, extCount, new_ext_count);
    int r = real_vkCreateDevice(phys, ci_copy, pAlloc, pDev);
    if (dbg_on()) fprintf(stderr, "[vkfix16] vkCreateDevice -> %d (0x%x)\n", r, (unsigned)r);
    return r;
}

/* Hook replacing vkGetInstanceProcAddr.  It is the entry point libMediaSDK
 * uses to populate its BSS vk* function-pointer variables, so intercepting it
 * lets us inject our fixed vkCreateDevice (and this same hook) wherever the
 * loader would otherwise install the originals. */
static void* my_vkGetInstanceProcAddr(void* instance, const char* name) {
    if (!name) return 0;
    if (strcmp(name, "vkCreateDevice") == 0) return (void*)my_vkCreateDevice;
    if (strcmp(name, "vkGetInstanceProcAddr") == 0) return (void*)my_vkGetInstanceProcAddr;
    /* For all other device functions, dispatch through the real loader so we
     * return the driver terminator (not the loader trampoline).  Returning the
     * trampoline here would recurse when the loader fills the device dispatch
     * table, so we must hand back the real implementation. */
    if (real_vkGetInstanceProcAddr) {
        void* f = real_vkGetInstanceProcAddr(instance, name);
        if (f) return f;
    }
    if (vk_handle) {
        void* f = real_dlsym(vk_handle, name);
        if (f) return f;
    }
    return 0;
}

/* Constructor: resolve the real dlopen/dlsym and the real Vulkan functions.
 * dlvsym(RTLD_NEXT, ..., "GLIBC_2.2.5") skips our own interposition wrappers
 * and picks up the versioned libc symbols directly.  We also preload
 * libvulkan.so.1 here so dlsym has a handle to resolve vk* entry points from. */
static void init_reals(void) __attribute__((constructor));
static void init_reals(void) {
    real_dlopen = (void*(*)(const char*,int))dlvsym(RTLD_NEXT, "dlopen", "GLIBC_2.2.5");
    real_dlsym = (void*(*)(void*,const char*))dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
    vk_handle = real_dlopen("libvulkan.so.1", RTLD_NOW | RTLD_GLOBAL);
    if (vk_handle) {
        real_vkGetInstanceProcAddr = (void*(*)(void*,const char*))real_dlsym(vk_handle, "vkGetInstanceProcAddr");
        real_vkCreateDevice = (int(*)(void*,const void*,const void*,void**))real_dlsym(vk_handle, "vkCreateDevice");
    }
    if (dbg_on()) fprintf(stderr, "[vkfix16] init: vk_handle=%p\n", vk_handle);
}
/* Interpose dlopen/dlsym.  The constructor may not have run yet when the
 * process's own dlopen/dlsym calls happen first, so both wrappers ensure the
 * real pointers are initialized before delegating. */
void* dlopen(const char* filename, int flags) {
    if (!real_dlopen) init_reals();
    return real_dlopen(filename, flags);
}

/* Interpose dlsym: redirect vkGetInstanceProcAddr and vkCreateDevice to our
 * hooks so libMediaSDK's BSS vk* pointers get the fixed implementations, and
 * pass every other lookup through to the real dlsym. */
void* dlsym(void* handle, const char* symbol) {
    if (!real_dlsym) init_reals();
    void* r = real_dlsym(handle, symbol);
    if (symbol && strcmp(symbol, "vkGetInstanceProcAddr") == 0) return (void*)my_vkGetInstanceProcAddr;
    if (symbol && strcmp(symbol, "vkCreateDevice") == 0) return (void*)my_vkCreateDevice;
    return r;
}
