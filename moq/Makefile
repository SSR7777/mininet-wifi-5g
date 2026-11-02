# Build moqpico (MoQ / MOQT draft-16 on picoquic) against an existing picoquic build.
#   make                      (picoquic in ~/picoquic, built with install_picoquic.sh)
#   make PICOQUIC=/path/to/picoquic
PICOQUIC ?= $(HOME)/picoquic
PTLS_BUILD := $(firstword $(wildcard $(PICOQUIC)/_deps/picotls-build) $(wildcard $(PICOQUIC)/../picotls))
PTLS_INC := $(firstword $(wildcard $(PICOQUIC)/_deps/picotls-src/include) $(wildcard $(PICOQUIC)/../picotls/include))

CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
CPPFLAGS += -I$(PICOQUIC)/picoquic -I$(PICOQUIC)/loglib -I$(PTLS_INC)
PTLS_LIBS := $(wildcard $(PTLS_BUILD)/libpicotls-openssl.a) $(wildcard $(PTLS_BUILD)/libpicotls-minicrypto.a) \
             $(wildcard $(PTLS_BUILD)/libpicotls-fusion.a) $(wildcard $(PTLS_BUILD)/libpicotls-core.a)
LIBS := $(PICOQUIC)/libpicoquic-log.a $(PICOQUIC)/libpicoquic-core.a $(PTLS_LIBS) \
        $(PICOQUIC)/libpicoquic-core.a $(PTLS_LIBS) -lssl -lcrypto -lpthread -lm

all: moqpico

moqpico: moqpico.o moqt.o
	$(CC) -o $@ $^ $(LIBS)

%.o: %.c moqt.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $<

clean:
	rm -f *.o moqpico

.PHONY: all clean
