// agentd-krun: boot one agentd sandbox microVM with libkrun.
//
// The sandbox gets no network: the implicit vsock device (which enables TSI,
// i.e. transparent proxying of AF_INET sockets through the host) is disabled
// and replaced by a vsock device with TSI off, and no virtio-net device is
// added. The only way in or out is a single vsock port that the *host*
// dials: libkrun listens on --vsock-sock and forwards each host connection to
// --vsock-port inside the sandbox.
//
// With --root-ro the root is shared read-only, so one base image serves every
// sandbox; sandboxd layers a per-session tmpfs overlay on top inside the VM.
// --overlay-dir / --inject add virtual directories and files (backed by this
// process's memory) to the root, which is how agentd's own sandbox-side code
// gets in without modifying the base image.
//
// krun_start_enter() takes over this process and exits with the workload's
// exit code, so agentd runs one launcher process per sandbox.
//
// usage: agentd-krun --root DIR [--root-ro] --vsock-port N --vsock-sock PATH
//                    [--share TAG=HOSTDIR]... [--share-ro TAG=HOSTDIR]... [--env K=V]...
//                    [--overlay-dir PATH]... [--inject PATH=HOSTFILE]...
//                    [--cpus N] [--mem MIB] [--workdir DIR]
//                    -- EXEC [ARG]...

#include <libkrun.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_ITEMS 64

static void die(const char *what, int rc) {
    fprintf(stderr, "agentd-krun: %s failed: %d\n", what, rc);
    exit(125);
}

// Read a whole host file into memory that stays valid for the VM's lifetime
// (libkrun does not copy overlay file contents).
static uint8_t *slurp(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(125); }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *buf = malloc(n > 0 ? (size_t)n : 1);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) { perror(path); exit(125); }
    fclose(f);
    *len = (size_t)n;
    return buf;
}

int main(int argc, char **argv) {
    const char *root = NULL, *vsock_sock = NULL, *workdir = "/";
    const char *shares[MAX_ITEMS], *overlay_dirs[MAX_ITEMS], *injects[MAX_ITEMS];
    bool share_ro[MAX_ITEMS];
    const char *envp[MAX_ITEMS + 1];
    int nshares = 0, nenv = 0, ndirs = 0, ninjects = 0, cpus = 2, mem = 1024, vsock_port = -1;
    bool root_ro = false;
    int i;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--")) { i++; break; }
        if (!strcmp(argv[i], "--root-ro")) { root_ro = true; continue; }
        if (i + 1 >= argc) { fprintf(stderr, "missing value for %s\n", argv[i]); return 2; }
        const char *v = argv[++i];
        const char *flag = argv[i - 1];
        if (!strcmp(flag, "--root")) root = v;
        else if (!strcmp(flag, "--vsock-sock")) vsock_sock = v;
        else if (!strcmp(flag, "--vsock-port")) vsock_port = atoi(v);
        else if (!strcmp(flag, "--cpus")) cpus = atoi(v);
        else if (!strcmp(flag, "--mem")) mem = atoi(v);
        else if (!strcmp(flag, "--workdir")) workdir = v;
        else if (!strcmp(flag, "--share") && nshares < MAX_ITEMS) { share_ro[nshares] = false; shares[nshares++] = v; }
        else if (!strcmp(flag, "--share-ro") && nshares < MAX_ITEMS) { share_ro[nshares] = true; shares[nshares++] = v; }
        else if (!strcmp(flag, "--env") && nenv < MAX_ITEMS) envp[nenv++] = v;
        else if (!strcmp(flag, "--overlay-dir") && ndirs < MAX_ITEMS) overlay_dirs[ndirs++] = v;
        else if (!strcmp(flag, "--inject") && ninjects < MAX_ITEMS) injects[ninjects++] = v;
        else { fprintf(stderr, "unknown flag %s\n", flag); return 2; }
    }
    envp[nenv] = NULL;
    if (!root || !vsock_sock || vsock_port < 0 || i >= argc) {
        fprintf(stderr, "usage: agentd-krun --root DIR --vsock-port N --vsock-sock PATH "
                        "[--share TAG=DIR]... [--env K=V]... -- EXEC [ARG]...\n");
        return 2;
    }

    int rc;
    int ctx = krun_create_ctx();
    if (ctx < 0) die("krun_create_ctx", ctx);
    if ((rc = krun_set_vm_config(ctx, (uint8_t)cpus, (uint32_t)mem))) die("krun_set_vm_config", rc);
    if (root_ro) {
        if ((rc = krun_add_virtiofs3(ctx, KRUN_FS_ROOT_TAG, root, 0, true))) die("krun_add_virtiofs3(root)", rc);
    } else if ((rc = krun_set_root(ctx, root))) {
        die("krun_set_root", rc);
    }
    for (int d = 0; d < ndirs; d++)
        if ((rc = krun_fs_add_overlay_dir(ctx, KRUN_FS_ROOT_TAG, overlay_dirs[d], 040755)))
            die("krun_fs_add_overlay_dir", rc);
    for (int f = 0; f < ninjects; f++) {
        char *spec = strdup(injects[f]);
        char *eq = strchr(spec, '=');
        if (!eq) { fprintf(stderr, "--inject expects PATH=HOSTFILE\n"); return 2; }
        *eq = '\0';
        size_t len;
        uint8_t *data = slurp(eq + 1, &len);
        if ((rc = krun_fs_add_overlay_file(ctx, KRUN_FS_ROOT_TAG, spec, data, len, 0100644, false)))
            die("krun_fs_add_overlay_file", rc);
    }

    for (int s = 0; s < nshares; s++) {
        char *spec = strdup(shares[s]);
        char *eq = strchr(spec, '=');
        if (!eq) { fprintf(stderr, "--share expects TAG=HOSTDIR\n"); return 2; }
        *eq = '\0';
        // Read-only shares are enforced by libkrun on the host side.
        if ((rc = krun_add_virtiofs3(ctx, spec, eq + 1, 0, share_ro[s]))) die("krun_add_virtiofs3", rc);
    }

    // Zero egress: no TSI, no NIC. The host-dialed port is the only channel.
    if ((rc = krun_disable_implicit_vsock(ctx))) die("krun_disable_implicit_vsock", rc);
    if ((rc = krun_add_vsock(ctx, 0))) die("krun_add_vsock", rc);
    if ((rc = krun_add_vsock_port2(ctx, (uint32_t)vsock_port, vsock_sock, true)))
        die("krun_add_vsock_port2", rc);

    if ((rc = krun_set_workdir(ctx, workdir))) die("krun_set_workdir", rc);
    if ((rc = krun_set_exec(ctx, argv[i], (const char *const *)&argv[i + 1],
                            (const char *const *)envp)))
        die("krun_set_exec", rc);

    rc = krun_start_enter(ctx);
    die("krun_start_enter", rc);
    return 125;
}
