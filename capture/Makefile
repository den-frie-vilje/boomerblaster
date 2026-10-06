# The capture helper. `make` builds it here; `make test` runs the end-to-end
# tests against the build (python3, stdlib only). boomerblaster init builds
# it the same way when no built copy is found.
CC ?= clang
CFLAGS ?= -O2 -Wall -Wextra -Wno-unused-parameter -fobjc-arc -mmacosx-version-min=12.0
FRAMEWORKS = -framework Foundation -framework AVFoundation -framework CoreAudio -framework AudioToolbox

boomerblaster-capture: boomerblaster-capture.m Info.plist
	$(CC) $(CFLAGS) $(FRAMEWORKS) -sectcreate __TEXT __info_plist Info.plist -o $@ $<

.PHONY: test clean
test: boomerblaster-capture
	python3 test_capture.py

clean:
	rm -f boomerblaster-capture
