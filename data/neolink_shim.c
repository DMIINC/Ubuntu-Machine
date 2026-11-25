/*
 * neolink_shim: makes neolink 0.6.3-rc.2 start a battery camera's video without
 * its 20 s stall. Loaded into neolink with LD_PRELOAD by Reolink Viewer.
 *
 * Build:  gcc -shared -fPIC -O2 -o neolink_shim.so neolink_shim.c -ldl
 * (install.sh does this.)   Switch off:  REOLINK_VIEWER_NEOLINK_SHIM=0
 *
 * What goes wrong without it (read in neolink's and GStreamer's source):
 *
 *   For every RTSP client neolink sets its "appsrc" element to PAUSED by itself
 *   and at once pushes a keyframe into it. gst-rtsp-server starts the rest of
 *   that client's pipeline a few milliseconds later. If the keyframe gets there
 *   first, the appsrc's task stops for good, no data ever reaches the client,
 *   and the server waits its fixed 20 s before answering 503. neolink serves all
 *   clients from one thread, so every other client waits behind it. The first
 *   pipeline of a process is the slowest to start, and the viewer starts neolink
 *   afresh for every wake, so the first connection usually loses this race.
 *
 *   Even when it wins, neolink has the server reset each client's pipeline
 *   between the RTSP DESCRIBE and PLAY requests, which costs two keyframe
 *   intervals (about 4 s) per connection.
 *
 * What this changes, by standing in for three GStreamer functions when neolink
 * itself calls them (calls from GStreamer's own libraries pass through):
 *
 *   a) gst_element_set_state(): neolink's own PAUSED/PLAYING requests on its
 *      "vidsrc" / "audsrc" elements are ignored, so the appsrc starts together
 *      with its pipeline and nothing can be pushed too early.
 *   b) gst_app_src_set_stream_type(): "stream" instead of "seekable", so frames
 *      pushed before the pipeline starts are kept instead of thrown away.
 *   c) gst_rtsp_media_factory_set_suspend_mode(): "none" instead of "reset", so
 *      PLAY doesn't have to wait for a second keyframe.
 *
 * All three are needed. In an emulation of neolink's RTSP side the first frame
 * then arrived 0.1 s after connecting instead of 4 s (or 24 s after a stall).
 * It had not been run against the real neolink binary when this was written:
 * the viewer logs each wake's timeline, and stops using the library by itself if
 * a wake fails with it.
 *
 * It is tied to neolink 0.6.3-rc.2 (the element names and these three calls).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define GST_STATE_PAUSED 3
#define GST_STATE_CHANGE_SUCCESS 1
#define GST_APP_STREAM_TYPE_STREAM 0
#define GST_RTSP_SUSPEND_MODE_NONE 0

static uintptr_t main_start, main_end; /* where the main program (neolink) is loaded */
static int any_caller;                 /* for tests, where the caller isn't the main program */

static int find_main_program(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    (void)data;
    /* The first entry is the main program. */
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *segment = &info->dlpi_phdr[i];
        if (segment->p_type != PT_LOAD)
            continue;
        uintptr_t start = info->dlpi_addr + segment->p_vaddr;
        uintptr_t end = start + segment->p_memsz;
        if (!main_start || start < main_start)
            main_start = start;
        if (end > main_end)
            main_end = end;
    }
    return 1; /* stop after the first entry */
}

__attribute__((constructor)) static void neolink_shim_init(void)
{
    dl_iterate_phdr(find_main_program, NULL);
    const char *any = getenv("NEOLINK_SHIM_ANY_CALLER");
    any_caller = any && *any == '1';
    /* The viewer looks for this line to know the library is in place. */
    fprintf(stderr, "neolink_shim: active\n");
    fflush(stderr);
}

/* Whether the call comes from neolink's own code rather than from a library. */
static int from_neolink(void *return_address)
{
    uintptr_t address = (uintptr_t)return_address;
    return any_caller || (address >= main_start && address < main_end);
}

/* c: keep each client's pipeline running between DESCRIBE and PLAY */
void gst_rtsp_media_factory_set_suspend_mode(void *factory, int mode)
{
    static void (*real)(void *, int);
    if (!real)
        real = dlsym(RTLD_NEXT, "gst_rtsp_media_factory_set_suspend_mode");
    if (real)
        real(factory, from_neolink(__builtin_return_address(0)) ? GST_RTSP_SUSPEND_MODE_NONE : mode);
}

/* b: no flushing seek when the appsrc starts, so early frames are kept */
void gst_app_src_set_stream_type(void *appsrc, int type)
{
    static void (*real)(void *, int);
    if (!real)
        real = dlsym(RTLD_NEXT, "gst_app_src_set_stream_type");
    if (real)
        real(appsrc, from_neolink(__builtin_return_address(0)) ? GST_APP_STREAM_TYPE_STREAM : type);
}

/* a: ignore neolink's own PAUSED/PLAYING on its appsrc ("vidsrc" / "audsrc") */
int gst_element_set_state(void *element, int state)
{
    static int (*real)(void *, int);
    static char *(*get_name)(void *);
    static void (*free_name)(void *);
    if (!real) {
        real = dlsym(RTLD_NEXT, "gst_element_set_state");
        get_name = dlsym(RTLD_DEFAULT, "gst_object_get_name");
        free_name = dlsym(RTLD_DEFAULT, "g_free");
    }
    if (!real)
        return 0; /* GST_STATE_CHANGE_FAILURE */
    if (state >= GST_STATE_PAUSED && get_name && free_name
        && from_neolink(__builtin_return_address(0))) {
        char *name = get_name(element);
        int neolink_appsrc = name && (!strcmp(name, "vidsrc") || !strcmp(name, "audsrc"));
        free_name(name);
        if (neolink_appsrc)
            return GST_STATE_CHANGE_SUCCESS;
    }
    return real(element, state);
}
