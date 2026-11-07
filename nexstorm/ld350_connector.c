// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Lightning Monitor NexStorm Connector: a stand-in for Boltek's LD350_DLL.dll.
 *
 * NexStorm loads LD350_DLL.dll and calls the functions in Boltek's LD-350 Programmers Toolkit
 * (LD350.h). This DLL has the same functions, but instead of opening the LD-350 on this PC's USB it
 * connects to a detector shared on the network by Lightning Monitor (or Lightning Monitor Bridge),
 * using Lightning Monitor's sharing protocol. The detector stays on the Mac; NexStorm is one more
 * viewer beside Lightning Monitor's own.
 *
 * Settings come from nexstorm-connector.ini in %APPDATA%\Lightning Monitor Bridge (written by the
 * bridge's "nexstorm" command), or the file named by LD350_CONNECTOR_CONFIG:
 *   host = My-Mac.local
 *   port = 10001
 *   token = ...
 *   allow_settings = 0      ; 1 lets NexStorm change the detector's settings for every viewer
 *   squelch_scale = auto    ; auto, ld250 or ld350: how NexStorm's squelch is turned into the LD-350's SQ
 * The log goes to nexstorm-connector.log beside it (or LD350_CONNECTOR_LOG). The token is never logged.
 *
 * A shared LD-250 is presented as an LD-350 (NexStorm's receiver model stays LD-350): its $WIMST status becomes
 * $WIMSU, its ":SQUELCH n" reply becomes ":SQ" on the LD-350 scale, GPS and EFM-100 lines are dropped, and
 * NexStorm's squelch is converted back to the LD-250's 0-15. There are no waveforms, so strikes are unclassified.
 *
 * The Windows build is 32-bit, like NexStorm, and exports the toolkit's C++ decorated names through
 * ld350_connector.def. Other builds (used by the tests) export the plain C names.
 */

#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
typedef SRWLOCK connector_lock;
#define LOCK_INITIALIZER SRWLOCK_INIT
#define lock_acquire(lock) AcquireSRWLockExclusive(lock)
#define lock_release(lock) ReleaseSRWLockExclusive(lock)
typedef HANDLE connector_thread;
#define EXPORT
#else
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
typedef int SOCKET;
#define INVALID_SOCKET (-1)
#define closesocket close
typedef pthread_mutex_t connector_lock;
#define LOCK_INITIALIZER PTHREAD_MUTEX_INITIALIZER
#define lock_acquire(lock) pthread_mutex_lock(lock)
#define lock_release(lock) pthread_mutex_unlock(lock)
typedef pthread_t connector_thread;
#define EXPORT __attribute__((visibility("default")))
#endif

#define CONNECTOR_NAME "Lightning Monitor NexStorm Connector"
#ifndef CONNECTOR_VERSION
#define CONNECTOR_VERSION "dev"
#endif

/* LD350.h */
#define MAX_MESSAGE_LEN 50
#define CAPTURE_BUFFERSIZE 512
#define LTS3_BUFFERSIZE 167

typedef struct {
    char message[MAX_MESSAGE_LEN];
} LD350_MessageDataType;

typedef struct {
    int East[CAPTURE_BUFFERSIZE];
    int North[CAPTURE_BUFFERSIZE];
    int E_Field[CAPTURE_BUFFERSIZE];
    unsigned char LTS3_Data[LTS3_BUFFERSIZE];
} LD350_CaptureDataType;

/* The LD-350's USB capture frame: "[", four bytes per sample, then "]" or the 167-byte LTS-3 block. */
#define SAMPLE_BYTES (CAPTURE_BUFFERSIZE * 4)
#define FULL_FRAME_BYTES (SAMPLE_BYTES + LTS3_BUFFERSIZE)
#define LAYOUT_DECISION_BYTES (SAMPLE_BYTES + 14)
#define MAX_LINE 1024
#define MAX_COMMAND 256

#define MESSAGE_QUEUE 512
#define CAPTURE_QUEUE 64
#define CONNECT_SECONDS 4
#define GREETING_SECONDS 6
#define RETRY_SECONDS 3
#define MAX_LOG_BYTES (1 << 20)

typedef enum { FRAMER_TEXT, FRAMER_CAPTURE } framer_state;

static struct {
    connector_lock lock;
    connector_lock log_lock;
    int open;
    volatile int stopping;
    int thread_started;
    connector_thread thread;
    SOCKET socket;
    int connected;

    char host[256];
    char port[16];
    char token[256];
    int allow_settings;
    char squelch_scale[8];
    char config_path[1024];
    char log_path[1024];

    int raw_wanted;
    int ld250;
    int message_head, message_count;
    int capture_head, capture_count;
    unsigned long dropped_captures;

    framer_state framer;
    unsigned char frame[FULL_FRAME_BYTES];
    int frame_length;
    char line[MAX_LINE + 1];
    int line_length;
} connector = {.lock = LOCK_INITIALIZER, .log_lock = LOCK_INITIALIZER, .socket = INVALID_SOCKET};

/* Kept out of `connector`, whose non-zero initial socket would otherwise store them in the DLL file. */
static LD350_MessageDataType messages[MESSAGE_QUEUE];
static LD350_CaptureDataType captures[CAPTURE_QUEUE];

/* Logging */

static void log_message(const char *format, ...) {
    if (!connector.log_path[0]) return;
    lock_acquire(&connector.log_lock);
    FILE *file = fopen(connector.log_path, "a");
    if (file) {
        time_t now = time(NULL);
        struct tm local;
#ifdef _WIN32
        localtime_s(&local, &now);
#else
        localtime_r(&now, &local);
#endif
        char stamp[32];
        strftime(stamp, sizeof stamp, "%Y-%m-%d %H:%M:%S", &local);
        fprintf(file, "%s  ", stamp);
        va_list arguments;
        va_start(arguments, format);
        vfprintf(file, format, arguments);
        va_end(arguments);
        fputc('\n', file);
        fclose(file);
    }
    lock_release(&connector.log_lock);
}

static void rotate_log(void) {
    if (!connector.log_path[0]) return;
    FILE *file = fopen(connector.log_path, "rb");
    if (!file) return;
    fseek(file, 0, SEEK_END);
    long size = ftell(file);
    fclose(file);
    if (size > MAX_LOG_BYTES) {
        char old[sizeof connector.log_path + 8];
        snprintf(old, sizeof old, "%s.old", connector.log_path);
        remove(old);
        rename(connector.log_path, old);
    }
}

/* Settings */

static void copy_text(char *destination, size_t size, const char *source) {
    snprintf(destination, size, "%s", source ? source : "");
}

static char *trim(char *text) {
    while (*text && isspace((unsigned char)*text)) text++;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) *--end = '\0';
    return text;
}

static void make_folder(const char *path) {
#ifdef _WIN32
    CreateDirectoryA(path, NULL);
#else
    mkdir(path, 0700);
#endif
}

static void find_paths(void) {
    const char *config = getenv("LD350_CONNECTOR_CONFIG");
    const char *log = getenv("LD350_CONNECTOR_LOG");
    char folder[900] = "";
#ifdef _WIN32
    const char *appdata = getenv("APPDATA");
    if (appdata && *appdata) snprintf(folder, sizeof folder, "%s\\Lightning Monitor Bridge", appdata);
    const char *separator = "\\";
#else
    const char *separator = "/";
#endif
    if (config && *config) {
        copy_text(connector.config_path, sizeof connector.config_path, config);
    } else if (folder[0]) {
        snprintf(connector.config_path, sizeof connector.config_path, "%s%snexstorm-connector.ini", folder, separator);
    } else {
        connector.config_path[0] = '\0';
    }
    if (log && *log) {
        copy_text(connector.log_path, sizeof connector.log_path, log);
    } else if (folder[0]) {
        make_folder(folder);
        snprintf(connector.log_path, sizeof connector.log_path, "%s%snexstorm-connector.log", folder, separator);
    } else {
        connector.log_path[0] = '\0';
    }
}

static int load_settings(void) {
    connector.host[0] = '\0';
    copy_text(connector.port, sizeof connector.port, "10001");
    connector.token[0] = '\0';
    connector.allow_settings = 0;
    copy_text(connector.squelch_scale, sizeof connector.squelch_scale, "auto");
    if (!connector.config_path[0]) return 0;
    FILE *file = fopen(connector.config_path, "r");
    if (!file) return 0;
    char raw[1024];
    while (fgets(raw, sizeof raw, file)) {
        char *line = trim(raw);
        if (!*line || *line == ';' || *line == '#' || *line == '[') continue;
        char *equals = strchr(line, '=');
        if (!equals) continue;
        *equals = '\0';
        char *key = trim(line);
        char *value = trim(equals + 1);
        if (!strcmp(key, "host")) copy_text(connector.host, sizeof connector.host, value);
        else if (!strcmp(key, "port") && *value) copy_text(connector.port, sizeof connector.port, value);
        else if (!strcmp(key, "token")) copy_text(connector.token, sizeof connector.token, value);
        else if (!strcmp(key, "squelch_scale") && *value) copy_text(connector.squelch_scale, sizeof connector.squelch_scale, value);
        else if (!strcmp(key, "allow_settings")) connector.allow_settings = (*value == '1' || *value == 't' || *value == 'T' || *value == 'y' || *value == 'Y');
    }
    fclose(file);
    return 1;
}

/* Queues (callers hold connector.lock) */

static void queue_message(const char *text) {
    if (connector.message_count == MESSAGE_QUEUE) {
        connector.message_head = (connector.message_head + 1) % MESSAGE_QUEUE;
        connector.message_count--;
    }
    LD350_MessageDataType *slot = &messages[(connector.message_head + connector.message_count) % MESSAGE_QUEUE];
    /* Like Boltek's DLL, each message keeps its CR LF. */
    snprintf(slot->message, MAX_MESSAGE_LEN, "%.*s\r\n", MAX_MESSAGE_LEN - 3, text);
    connector.message_count++;
}

static void queue_capture(const unsigned char *samples, const unsigned char *lts3) {
    if (!connector.raw_wanted) return;
    if (connector.capture_count == CAPTURE_QUEUE) {
        connector.capture_head = (connector.capture_head + 1) % CAPTURE_QUEUE;
        connector.capture_count--;
        connector.dropped_captures++;
    }
    LD350_CaptureDataType *slot = &captures[(connector.capture_head + connector.capture_count) % CAPTURE_QUEUE];
    for (int index = 0; index < CAPTURE_BUFFERSIZE; index++) {
        const unsigned char *sample = samples + index * 4;
        slot->East[index] = (sample[0] & 0x0F) << 8 | sample[1];
        slot->E_Field[index] = sample[2];
        slot->North[index] = (sample[0] >> 4) << 8 | sample[3];
    }
    if (lts3) memcpy(slot->LTS3_Data, lts3, LTS3_BUFFERSIZE);
    else memset(slot->LTS3_Data, 0, LTS3_BUFFERSIZE);
    connector.capture_count++;
}

static void clear_queues(void) {
    connector.message_head = connector.message_count = 0;
    connector.capture_head = connector.capture_count = 0;
}

/* Framing: the same rules as Lightning Monitor's LD350StreamFramer (callers hold connector.lock). */

static void reset_framer(void) {
    connector.framer = FRAMER_TEXT;
    connector.frame_length = 0;
    connector.line_length = 0;
}

/*
 * The LD-350's SQ is a 0-100 trigger level; the StormTracker and LD-250 squelch is 0-15. NexStorm converts its own
 * squelch before sending it (its 10 arrives as SQ 76), so this only catches a 0-15 value sent as it is.
 * Boltek's LD350.h gives the pairs below; values between them are interpolated.
 */
static int ld350_trigger_level(int squelch) {
    static const int pairs[][2] = {{0, 40}, {1, 43}, {2, 47}, {4, 53}, {8, 58}, {15, 78}};
    if (squelch <= 0) return 40;
    if (squelch >= 15) return 78;
    for (int index = 1; index < 6; index++) {
        if (squelch <= pairs[index][0]) {
            int x0 = pairs[index - 1][0], y0 = pairs[index - 1][1], x1 = pairs[index][0], y1 = pairs[index][1];
            int numerator = (y1 - y0) * (squelch - x0), denominator = x1 - x0;
            return y0 + (2 * numerator + denominator) / (2 * denominator);
        }
    }
    return 78;
}

/* Whether NexStorm's SQ value is on the 0-15 scale: always (ld250), never (ld350), or when it is 15 or less (auto),
 * since an LD-350 trigger level that low makes the detector trigger on background noise. */
static int is_ld250_squelch(int value) {
    if (!strcmp(connector.squelch_scale, "ld350")) return 0;
    if (!strcmp(connector.squelch_scale, "ld250")) return 1;
    return value <= 15;
}

/* The LD-250 squelch (0-15) whose LD-350 trigger level is nearest to `level`. */
static int ld250_squelch(int level) {
    int best = 0;
    for (int squelch = 1; squelch <= 15; squelch++) {
        if (abs(ld350_trigger_level(squelch) - level) < abs(ld350_trigger_level(best) - level)) best = squelch;
    }
    return best;
}

/* NMEA checksum: XOR of the characters between "$" and "*". */
static unsigned nmea_checksum(const char *body) {
    unsigned checksum = 0;
    for (const char *character = body + 1; *character && *character != '*'; character++) checksum ^= (unsigned char)*character;
    return checksum;
}

/* An LD-250 line as NexStorm's LD-350 mode expects it, written to `out`; returns 0 to drop the line. */
static int ld250_as_ld350(const char *line, char *out, size_t size) {
    if (!strncmp(line, "$WIMST,", 7)) {
        /* $WIMST,close rate,total rate,close alarm,severe alarm,heading -> $WIMSU,...,LDN 1,LDN 2 */
        char fields[4][16] = {"0", "0", "0", "0"};
        const char *cursor = line + 7;
        for (int index = 0; index < 4 && *cursor && *cursor != '*'; index++) {
            size_t length = strcspn(cursor, ",*");
            snprintf(fields[index], sizeof fields[index], "%.*s", (int)(length < 15 ? length : 15), cursor);
            cursor += length;
            if (*cursor == ',') cursor++;
        }
        char body[96];
        snprintf(body, sizeof body, "$WIMSU,%s,%s,%s,%s,0,0", fields[0], fields[1], fields[2], fields[3]);
        snprintf(out, size, "%s*%02X", body, nmea_checksum(body));
        return 1;
    }
    if (!strncmp(line, ":SQUELCH", 8)) {
        snprintf(out, size, ":SQ %d", ld350_trigger_level(atoi(line + 8)));
        return 1;
    }
    /* Relayed GPS sentences and EFM-100 field readings mean nothing to NexStorm's LD-350 mode. */
    if (!strncmp(line, "$GP", 3) || !strncmp(line, "$GN", 3) || !strncmp(line, "$+", 2) || !strncmp(line, "$-", 2)) return 0;
    snprintf(out, size, "%s", line);
    return 1;
}

static void finish_line(void) {
    connector.line[connector.line_length] = '\0';
    connector.line_length = 0;
    const char *line = connector.line;
    /* The server's own lines are for the connector, not NexStorm. */
    if (!strncmp(line, ":SERVER", 7) || !strncmp(line, ":ERROR", 6)) {
        log_message("Server: %s", line);
        return;
    }
    /* A plain bridge doesn't say which model it shares; an LD-250's status line does. */
    if (!connector.ld250 && !strncmp(line, "$WIMST,", 7)) {
        connector.ld250 = 1;
        log_message("The shared detector is an LD-250: its lines are converted for NexStorm's LD-350 mode");
    }
    if (connector.ld250) {
        char converted[MAX_LINE + 1];
        if (ld250_as_ld350(line, converted, sizeof converted)) queue_message(converted);
        return;
    }
    queue_message(line);
}

static int has_timestamp_block(void) {
    const unsigned char *block = connector.frame + SAMPLE_BYTES;
    if (!memcmp(block + 10, "@@Ha", 4)) return 1;
    return block[0] != ']';
}

static void consume(unsigned char byte) {
    if (connector.framer == FRAMER_TEXT) {
        if (byte == '\r' || byte == '\n') {
            if (connector.line_length) finish_line();
        } else if (byte == '\0') {
        } else if (byte == '[' && connector.line_length == 0) {
            connector.framer = FRAMER_CAPTURE;
            connector.frame_length = 0;
        } else if (connector.line_length < MAX_LINE) {
            connector.line[connector.line_length++] = (char)byte;
        }
        return;
    }
    connector.frame[connector.frame_length++] = byte;
    if (connector.frame_length == LAYOUT_DECISION_BYTES && !has_timestamp_block()) {
        unsigned char leftover[LAYOUT_DECISION_BYTES - SAMPLE_BYTES - 1];
        memcpy(leftover, connector.frame + SAMPLE_BYTES + 1, sizeof leftover);
        queue_capture(connector.frame, NULL);
        connector.framer = FRAMER_TEXT;
        connector.frame_length = 0;
        for (size_t index = 0; index < sizeof leftover; index++) consume(leftover[index]);
    } else if (connector.frame_length == FULL_FRAME_BYTES) {
        queue_capture(connector.frame, connector.frame + SAMPLE_BYTES);
        connector.framer = FRAMER_TEXT;
        connector.frame_length = 0;
    }
}

/* Network */

static void close_socket(SOCKET socket) {
    if (socket != INVALID_SOCKET) {
#ifdef _WIN32
        shutdown(socket, SD_BOTH);
#else
        shutdown(socket, SHUT_RDWR);
#endif
        closesocket(socket);
    }
}

static void set_blocking(SOCKET socket, int blocking) {
#ifdef _WIN32
    u_long nonblocking = blocking ? 0 : 1;
    ioctlsocket(socket, FIONBIO, &nonblocking);
#else
    int flags = fcntl(socket, F_GETFL, 0);
    fcntl(socket, F_SETFL, blocking ? flags & ~O_NONBLOCK : flags | O_NONBLOCK);
#endif
}

static int wait_readable(SOCKET socket, int milliseconds) {
    fd_set readable;
    FD_ZERO(&readable);
    FD_SET(socket, &readable);
    struct timeval timeout = {milliseconds / 1000, (milliseconds % 1000) * 1000};
    return select((int)socket + 1, &readable, NULL, NULL, &timeout);
}

static int send_all(SOCKET socket, const char *data, size_t length) {
#ifdef MSG_NOSIGNAL
    const int flags = MSG_NOSIGNAL;
#else
    const int flags = 0;
#endif
    while (length > 0) {
        int sent = (int)send(socket, data, (int)length, flags);
        if (sent <= 0) return 0;
        data += sent;
        length -= (size_t)sent;
    }
    return 1;
}

static SOCKET connect_to(const char *host, const char *port, char *error, size_t error_size) {
    struct addrinfo hints, *addresses = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    if (getaddrinfo(host, port, &hints, &addresses) != 0 || !addresses) {
        snprintf(error, error_size, "can't find %s on the network", host);
        return INVALID_SOCKET;
    }
    SOCKET result = INVALID_SOCKET;
    snprintf(error, error_size, "%s:%s did not answer", host, port);
    for (struct addrinfo *address = addresses; address && result == INVALID_SOCKET; address = address->ai_next) {
        SOCKET candidate = socket(address->ai_family, address->ai_socktype, address->ai_protocol);
        if (candidate == INVALID_SOCKET) continue;
#ifdef SO_NOSIGPIPE
        int one_value = 1;
        setsockopt(candidate, SOL_SOCKET, SO_NOSIGPIPE, (const char *)&one_value, sizeof one_value);
#endif
        set_blocking(candidate, 0);
        connect(candidate, address->ai_addr, (int)address->ai_addrlen);
        fd_set writable, failed;
        FD_ZERO(&writable);
        FD_ZERO(&failed);
        FD_SET(candidate, &writable);
        FD_SET(candidate, &failed);
        struct timeval timeout = {CONNECT_SECONDS, 0};
        int connected = 0;
        if (select((int)candidate + 1, NULL, &writable, &failed, &timeout) > 0 && FD_ISSET(candidate, &writable)) {
            int socket_error = 0;
            socklen_t length = sizeof socket_error;
            getsockopt(candidate, SOL_SOCKET, SO_ERROR, (char *)&socket_error, &length);
            connected = socket_error == 0;
            if (!connected) snprintf(error, error_size, "%s:%s refused the connection (is sharing on?)", host, port);
        }
        if (connected) {
            set_blocking(candidate, 1);
            int one = 1;
            setsockopt(candidate, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof one);
            setsockopt(candidate, SOL_SOCKET, SO_KEEPALIVE, (const char *)&one, sizeof one);
            result = candidate;
        } else {
            closesocket(candidate);
        }
    }
    freeaddrinfo(addresses);
    return result;
}

/*
 * Connects and waits for the ":SERVER <description>" greeting that Lightning Monitor and the bridge send
 * once the token is accepted. Plain bridges send none, so a connection that stays open without one is
 * used as it is. Bytes after the greeting are framed for NexStorm.
 */
static SOCKET connect_and_log_in(char *error, size_t error_size) {
    SOCKET socket = connect_to(connector.host, connector.port, error, error_size);
    if (socket == INVALID_SOCKET) return INVALID_SOCKET;
    connector.ld250 = 0;
    if (connector.token[0]) {
        char auth[sizeof connector.token + 8];
        int length = snprintf(auth, sizeof auth, "AUTH %s\r\n", connector.token);
        if (!send_all(socket, auth, (size_t)length)) {
            snprintf(error, error_size, "the connection closed while logging in");
            closesocket(socket);
            return INVALID_SOCKET;
        }
    }
    char received[4096];
    int have = 0;
    time_t deadline = time(NULL) + GREETING_SECONDS;
    while (time(NULL) < deadline && !connector.stopping) {
        if (wait_readable(socket, 200) <= 0) continue;
        int count = (int)recv(socket, received + have, (int)(sizeof received - 1 - have), 0);
        if (count <= 0) {
            snprintf(error, error_size, connector.token[0]
                     ? "the detector's computer closed the connection"
                     : "the detector's computer closed the connection: does it need a token?");
            closesocket(socket);
            return INVALID_SOCKET;
        }
        have += count;
        received[have] = '\0';
        char *end = strpbrk(received, "\r\n");
        if (!end && have < (int)sizeof received - 1) continue;
        size_t line_length = end ? (size_t)(end - received) : (size_t)have;
        if (!strncmp(received, ":ERROR", 6)) {
            snprintf(error, error_size, "refused: %.*s%s", (int)line_length - 7 > 0 ? (int)line_length - 7 : 0, received + 7,
                     strstr(received, "unauthorised") ? " (check the token)" : "");
            closesocket(socket);
            return INVALID_SOCKET;
        }
        if (!strncmp(received, ":SERVER", 7)) {
            log_message("Connected to %s:%s (%.*s)", connector.host, connector.port, (int)line_length - 1, received + 1);
            if (strstr(received, "LD-250") && (size_t)(strstr(received, "LD-250") - received) < line_length) {
                connector.ld250 = 1;
                log_message("The shared detector is an LD-250: its lines are converted for NexStorm's LD-350 mode");
            }
            lock_acquire(&connector.lock);
            reset_framer();
            for (int index = (int)line_length; index < have; index++) consume((unsigned char)received[index]);
            lock_release(&connector.lock);
            return socket;
        }
        break;
    }
    log_message("Connected to %s:%s (no greeting: a plain bridge)", connector.host, connector.port);
    lock_acquire(&connector.lock);
    reset_framer();
    for (int index = 0; index < have; index++) consume((unsigned char)received[index]);
    lock_release(&connector.lock);
    return socket;
}

static void sleep_milliseconds(int milliseconds) {
#ifdef _WIN32
    Sleep((DWORD)milliseconds);
#else
    struct timespec pause = {milliseconds / 1000, (long)(milliseconds % 1000) * 1000000L};
    nanosleep(&pause, NULL);
#endif
}

static void reader_loop(void) {
    char error[512] = "";
    char last_error[512] = "";
    unsigned char buffer[8192];
    while (!connector.stopping) {
        lock_acquire(&connector.lock);
        SOCKET socket = connector.socket;
        lock_release(&connector.lock);
        if (socket == INVALID_SOCKET) {
            for (int waited = 0; waited < RETRY_SECONDS * 10 && !connector.stopping; waited++) sleep_milliseconds(100);
            if (connector.stopping) break;
            socket = connect_and_log_in(error, sizeof error);
            if (socket == INVALID_SOCKET) {
                if (strcmp(error, last_error)) log_message("Reconnecting: %s", error);
                copy_text(last_error, sizeof last_error, error);
                continue;
            }
            last_error[0] = '\0';
            lock_acquire(&connector.lock);
            connector.socket = socket;
            connector.connected = 1;
            lock_release(&connector.lock);
        }
        int ready = wait_readable(socket, 250);
        if (ready == 0) continue;
        int count = ready > 0 ? (int)recv(socket, (char *)buffer, sizeof buffer, 0) : -1;
        lock_acquire(&connector.lock);
        if (count > 0) {
            for (int index = 0; index < count; index++) consume(buffer[index]);
        } else if (connector.socket == socket) {
            connector.socket = INVALID_SOCKET;
            connector.connected = 0;
            reset_framer();
            close_socket(socket);
            if (!connector.stopping) log_message("Connection to %s:%s lost; reconnecting", connector.host, connector.port);
        }
        lock_release(&connector.lock);
    }
}

#ifdef _WIN32
static DWORD WINAPI reader_thread(LPVOID unused) {
    (void)unused;
    reader_loop();
    return 0;
}
#else
static void *reader_thread(void *unused) {
    (void)unused;
    reader_loop();
    return NULL;
}
#endif

/* Commands */

/* A command without an argument only asks for a value, except these, which act on the detector. */
static int changes_the_detector(const char *word, int has_argument) {
    static const char *actions[] = {"SAVE", "READ", "FT", "D1", "D2", "RESET", NULL};
    if (has_argument) return 1;
    for (int index = 0; actions[index]; index++) {
        if (!strcmp(word, actions[index])) return 1;
    }
    return 0;
}

static int send_command(const char *text) {
    char command[MAX_COMMAND + 1];
    copy_text(command, sizeof command, text);
    char *line = trim(command);
    for (char *character = line; *character; character++) {
        if (*character == '\r' || *character == '\n') *character = ' ';
    }
    line = trim(line);
    if (!*line) return 1;
    char word[32];
    size_t word_length = strcspn(line, " \t");
    if (word_length >= sizeof word) word_length = sizeof word - 1;
    for (size_t index = 0; index < word_length; index++) word[index] = (char)toupper((unsigned char)line[index]);
    word[word_length] = '\0';
    const char *argument = trim(line + strcspn(line, " \t"));
    int has_argument = *argument != '\0';

    /* Lightning Monitor keeps the detector alive and asks for its status every second. */
    if (!strcmp(word, "KA") || !strcmp(word, "STATUS")) return 1;
    /* Waveforms stay on for every viewer; NexStorm's choice only decides whether it is handed them. */
    if (!strcmp(word, "RAW")) {
        lock_acquire(&connector.lock);
        if (has_argument) {
            connector.raw_wanted = *argument == '1';
            if (!connector.raw_wanted) connector.capture_head = connector.capture_count = 0;
        }
        queue_message(connector.raw_wanted ? ":RAW 1" : ":RAW 0");
        lock_release(&connector.lock);
        return 1;
    }
    const char *shown = strncmp(word, "LDNPASSWORD", 11) ? line : word;
    char translated[64] = "";
    const char *model = connector.ld250 ? "LD-250" : "LD-350";
    if (!strcmp(word, "SQ") && has_argument && isdigit((unsigned char)*argument)) {
        int squelch = atoi(argument);
        if (connector.ld250) {
            /* NexStorm's LD-350 mode sends the 0-100 trigger level; the LD-250 takes 0-15. */
            if (squelch > 15) snprintf(translated, sizeof translated, "SQ %d", ld250_squelch(squelch));
        } else if (is_ld250_squelch(squelch)) {
            snprintf(translated, sizeof translated, "SQ %d", ld350_trigger_level(squelch));
        }
    }
    if (changes_the_detector(word, has_argument) && !connector.allow_settings) {
        if (translated[0]) {
            log_message("Not sent, NexStorm may not change the detector's settings: %s (NexStorm squelch; %s on the %s)", shown, translated, model);
        } else {
            log_message("Not sent, NexStorm may not change the detector's settings: %s", shown);
        }
        return 1;
    }
    if (translated[0]) {
        log_message("NexStorm squelch %s is %s on the %s", argument, translated, model);
        line = translated;
    }

    char wire[MAX_COMMAND + 3];
    int length = snprintf(wire, sizeof wire, "%s\r\n", line);
    lock_acquire(&connector.lock);
    int sent = connector.socket != INVALID_SOCKET && send_all(connector.socket, wire, (size_t)length);
    lock_release(&connector.lock);
    if (sent) log_message("-> %s", strncmp(word, "LDNPASSWORD", 11) ? line : word);
    return sent;
}

/* LD350_DLL.dll's functions */

EXPORT int LD350_Open(void) {
    if (connector.open) return 1;
    find_paths();
    rotate_log();
    log_message("%s %s", CONNECTOR_NAME, CONNECTOR_VERSION);
    if (!load_settings() || !connector.host[0]) {
        log_message("No detector set: run \"lightning-bridge nexstorm connect HOST\" (settings: %s)",
                    connector.config_path[0] ? connector.config_path : "none");
        return 0;
    }
#ifdef _WIN32
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        log_message("Windows networking could not start");
        return 0;
    }
#endif
    lock_acquire(&connector.lock);
    clear_queues();
    reset_framer();
    connector.raw_wanted = 0;
    connector.dropped_captures = 0;
    lock_release(&connector.lock);
    connector.stopping = 0;

    char error[512];
    SOCKET socket = connect_and_log_in(error, sizeof error);
    if (socket == INVALID_SOCKET) {
        log_message("Could not connect to %s:%s: %s", connector.host, connector.port, error);
#ifdef _WIN32
        WSACleanup();
#endif
        return 0;
    }
    lock_acquire(&connector.lock);
    connector.socket = socket;
    connector.connected = 1;
    lock_release(&connector.lock);
    if (connector.allow_settings) log_message("NexStorm may change the detector's settings");

#ifdef _WIN32
    connector.thread = CreateThread(NULL, 0, reader_thread, NULL, 0, NULL);
    connector.thread_started = connector.thread != NULL;
#else
    connector.thread_started = pthread_create(&connector.thread, NULL, reader_thread, NULL) == 0;
#endif
    if (!connector.thread_started) {
        log_message("Could not start the connection thread");
        close_socket(socket);
        connector.socket = INVALID_SOCKET;
        connector.connected = 0;
        return 0;
    }
    connector.open = 1;
    return 1;
}

EXPORT int LD350_Close(void) {
    if (!connector.open) return 1;
    connector.stopping = 1;
    lock_acquire(&connector.lock);
    SOCKET socket = connector.socket;
    connector.socket = INVALID_SOCKET;
    connector.connected = 0;
    close_socket(socket);
    lock_release(&connector.lock);
    if (connector.thread_started) {
#ifdef _WIN32
        WaitForSingleObject(connector.thread, INFINITE);
        CloseHandle(connector.thread);
#else
        pthread_join(connector.thread, NULL);
#endif
        connector.thread_started = 0;
    }
    lock_acquire(&connector.lock);
    clear_queues();
    reset_framer();
    lock_release(&connector.lock);
#ifdef _WIN32
    WSACleanup();
#endif
    if (connector.dropped_captures) log_message("%lu waveforms were dropped because NexStorm fell behind", connector.dropped_captures);
    log_message("Closed");
    connector.open = 0;
    return 1;
}

/* Lightning Monitor keeps the detector alive; this reports whether the connection to it is up. */
EXPORT int LD350_KeepAlive(void) {
    lock_acquire(&connector.lock);
    int connected = connector.open && connector.connected;
    lock_release(&connector.lock);
    return connected;
}

EXPORT int LD350_Command(char *command) {
    if (!connector.open || !command) return 0;
    return send_command(command);
}

EXPORT int LD350_SetTriggerLevel(unsigned char level) {
    if (!connector.open) return 0;
    char command[16];
    snprintf(command, sizeof command, "SQ %u", (unsigned)level);
    return send_command(command);
}

EXPORT int LD350_ForceTrigger(void) {
    if (!connector.open) return 0;
    return send_command("FT");
}

EXPORT int LD350_RawDataOn(void) {
    if (!connector.open) return 0;
    lock_acquire(&connector.lock);
    connector.raw_wanted = 1;
    lock_release(&connector.lock);
    return 1;
}

EXPORT int LD350_RawDataOff(void) {
    if (!connector.open) return 0;
    lock_acquire(&connector.lock);
    connector.raw_wanted = 0;
    connector.capture_head = connector.capture_count = 0;
    lock_release(&connector.lock);
    return 1;
}

/*
 * The toolkit declares these as returning bool, but NexStorm reads the whole of EAX, so they return a full
 * int: 0 or 1 in every byte, which bool callers (reading AL) see the same way.
 */
EXPORT int LD350_MessageReady(void) {
    lock_acquire(&connector.lock);
    int ready = connector.message_count > 0;
    lock_release(&connector.lock);
    return ready;
}

EXPORT int LD350_CaptureReady(void) {
    lock_acquire(&connector.lock);
    int ready = connector.capture_count > 0;
    lock_release(&connector.lock);
    return ready;
}

EXPORT void LD350_GetMessageData(LD350_MessageDataType *message) {
    if (!message) return;
    lock_acquire(&connector.lock);
    if (connector.message_count > 0) {
        *message = messages[connector.message_head];
        connector.message_head = (connector.message_head + 1) % MESSAGE_QUEUE;
        connector.message_count--;
    } else {
        memset(message, 0, sizeof *message);
    }
    lock_release(&connector.lock);
}

EXPORT void LD350_GetCaptureData(LD350_CaptureDataType *capture) {
    if (!capture) return;
    lock_acquire(&connector.lock);
    if (connector.capture_count > 0) {
        *capture = captures[connector.capture_head];
        connector.capture_head = (connector.capture_head + 1) % CAPTURE_QUEUE;
        connector.capture_count--;
    } else {
        memset(capture, 0, sizeof *capture);
    }
    lock_release(&connector.lock);
}
