# Makefile — build the LD_PRELOAD shims for the Insta360 MediaSDK AMD path.
#
# Targets:
#   all        build shim/vkfix16.so and shim/amfshim3.so
#   verify     run ./apply_patch.sh --check (verify the patched tree, read-only;
#              uses the algorithmic check from libmedia_adapt.py)
#   clean      remove built shims
#
# CC/CFLAGS are overridable from the command line (make CC=... CFLAGS=...).
# -O2: optimize; -fPIC: required for a shared library; -Wall -Wextra: enable
# warnings (must stay warning-clean).
# -Wno-nonnull-compare: vkfix16.c intentionally checks `symbol` (declared
# nonnull by dlsym) for NULL in its interposition wrapper; the warning is
# known-harmless, so silence it.
CC      ?= gcc
CFLAGS  ?= -O2 -fPIC -Wall -Wextra -Wno-nonnull-compare
# -ldl: dlopen/dlsym/dlvsym are used by both shims.
LDLIBS   = -ldl

# Both shims are built as shared objects and loaded via LD_PRELOAD.
SHIMS = shim/vkfix16.so shim/amfshim3.so

.PHONY: all verify clean

all: $(SHIMS)

shim/vkfix16.so: shim/vkfix16.c
	$(CC) $(CFLAGS) -shared -o $@ $< $(LDLIBS)

shim/amfshim3.so: shim/amfshim3.c
	$(CC) $(CFLAGS) -shared -o $@ $< $(LDLIBS)

verify:
	./apply_patch.sh --check

clean:
	rm -f $(SHIMS)
