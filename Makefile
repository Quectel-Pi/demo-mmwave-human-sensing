SDK_DIR := $(abspath $(CURDIR)/../../..)
SRC := src
BUILD := build
APP := demo-mmwave-human-sensing
APP_TOOLCHAIN ?= $(SDK_DIR)/toolchains/gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu
APP_SYSROOT ?= $(APP_TOOLCHAIN)/aarch64-none-linux-gnu/libc
APP_CROSS_COMPILE ?= $(APP_TOOLCHAIN)/bin/aarch64-none-linux-gnu-
USER_CC ?= $(APP_CROSS_COMPILE)gcc
USER_AR ?= $(APP_CROSS_COMPILE)ar
APP_CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2
# Do not mix build.sh's Debian sysroot flags with the bundled compiler/libc.
APP_CPPFLAGS := --sysroot="$(APP_SYSROOT)" -I$(CURDIR)/$(BUILD)/uapi
APP_LDFLAGS := --sysroot="$(APP_SYSROOT)"
UAPI := $(BUILD)/uapi/linux/r60abd1.h
RUNTIME_FILES := $(BUILD)/$(APP) $(BUILD)/ui_server.py $(BUILD)/r60abd1-ui.html

.PHONY: all userspace clean
all: userspace
userspace: $(RUNTIME_FILES)

$(BUILD):
	mkdir -p "$@"

$(BUILD)/ui_server.py $(BUILD)/r60abd1-ui.html: $(BUILD)/%: $(SRC)/% | $(BUILD)
	cp "$<" "$@"

$(UAPI): $(SDK_DIR)/kernel/include/uapi/linux/r60abd1.h
	mkdir -p "$(@D)"
	cp "$<" "$@"

$(BUILD)/r60abd1_client.o: $(SRC)/r60abd1.c $(SRC)/r60abd1.h $(UAPI) Makefile
	$(USER_CC) $(APP_CPPFLAGS) $(APP_CFLAGS) -c "$<" -o "$@"

$(BUILD)/libr60abd1.a: $(BUILD)/r60abd1_client.o
	$(USER_AR) rcs "$@" "$<"

$(BUILD)/$(APP): $(SRC)/main.c $(SRC)/r60abd1.h $(UAPI) $(BUILD)/libr60abd1.a Makefile
	$(USER_CC) $(APP_CPPFLAGS) $(APP_CFLAGS) $(APP_LDFLAGS) $(SRC)/main.c $(BUILD)/libr60abd1.a $(LDLIBS) -o "$@"

clean:
	rm -f $(BUILD)/r60abd1_client.o $(BUILD)/libr60abd1.a $(RUNTIME_FILES) "$(UAPI)"
