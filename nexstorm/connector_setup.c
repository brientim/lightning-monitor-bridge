// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * NexStorm Connector Setup: installs the Lightning Monitor NexStorm Connector (connector_setup.rc embeds
 * LD350_DLL.dll) so NexStorm reads an LD-350 shared by Lightning Monitor, or by Lightning Monitor Bridge on
 * this PC, instead of opening the detector on this PC's USB.
 *
 *   NexStorm Connector Setup.exe                       menu
 *   NexStorm Connector Setup.exe /install /host=My-Mac.local [/port=10001] [/token=...] [/allow-settings]
 *                                [/folder="C:\Astrogenic\NexStorm"] [/no-start]
 *   NexStorm Connector Setup.exe /restore | /status
 *
 * Boltek's LD350_DLL.dll beside NexStorm.exe (the one NexStorm loads) and in x86 and x64 is kept as
 * LD350_DLL.dll.boltek; Restore puts it back. Settings go to %APPDATA%\Lightning Monitor Bridge\nexstorm-connector.ini,
 * the same file Lightning Monitor Bridge writes. An LD350_DLL.dll that is the connector, next to this program
 * (the signed copy in the bridge's nexstorm folder), is installed in preference to the embedded one.
 */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef CONNECTOR_VERSION
#define CONNECTOR_VERSION "dev"
#endif

#define MARKER "Lightning Monitor NexStorm Connector"
#define DLL_NAME "LD350_DLL.dll"
#define BACKUP_SUFFIX ".boltek"
#define CONNECTOR_RESOURCE 101
/* Room for a MAX_PATH folder plus a file name. */
#define PATH_SIZE (MAX_PATH + 64)

typedef struct {
    char host[256];
    char port[16];
    char token[256];
    int allow_settings;
    char squelch_scale[16];
} Settings;

static char nexstorm_folder[MAX_PATH] = "";
static char settings_path[MAX_PATH] = "";
static int interactive = 1;

/* Small helpers */

static char *trim(char *text) {
    while (*text && isspace((unsigned char)*text)) text++;
    char *end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) *--end = '\0';
    return text;
}

static void read_line(const char *prompt, char *buffer, size_t size, int hidden) {
    printf("%s", prompt);
    fflush(stdout);
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    DWORD mode = 0;
    if (hidden && GetConsoleMode(input, &mode)) SetConsoleMode(input, mode & ~ENABLE_ECHO_INPUT);
    if (!fgets(buffer, (int)size, stdin)) buffer[0] = '\0';
    if (hidden && mode) {
        SetConsoleMode(input, mode);
        printf("\n");
    }
    char *trimmed = trim(buffer);
    memmove(buffer, trimmed, strlen(trimmed) + 1);
}

static int ask_yes(const char *question, int default_yes) {
    if (!interactive) return default_yes;
    char answer[16];
    printf("%s [%s] ", question, default_yes ? "Y/n" : "y/N");
    read_line("", answer, sizeof answer, 0);
    if (!answer[0]) return default_yes;
    return answer[0] == 'y' || answer[0] == 'Y';
}

static void pause_before_exit(void) {
    if (!interactive) return;
    char ignored[8];
    read_line("\nPress Enter to close.", ignored, sizeof ignored, 0);
}

static int file_exists(const char *path) {
    DWORD attributes = GetFileAttributesA(path);
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

static int folder_exists(const char *path) {
    DWORD attributes = GetFileAttributesA(path);
    return attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_DIRECTORY);
}

static unsigned char *read_file(const char *path, DWORD *size) {
    HANDLE file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return NULL;
    DWORD length = GetFileSize(file, NULL), done = 0;
    unsigned char *data = (unsigned char *)malloc(length ? length : 1);
    if (data && !ReadFile(file, data, length, &done, NULL)) done = 0;
    CloseHandle(file);
    if (!data || done != length) {
        free(data);
        return NULL;
    }
    *size = length;
    return data;
}

static int contains(const unsigned char *data, DWORD size, const char *text) {
    size_t length = strlen(text);
    for (DWORD index = 0; index + length <= size; index++) {
        if (!memcmp(data + index, text, length)) return 1;
    }
    return 0;
}

static int is_connector(const char *path) {
    DWORD size = 0;
    unsigned char *data = read_file(path, &size);
    int result = data && contains(data, size, MARKER);
    free(data);
    return result;
}

static int write_file(const char *path, const void *data, DWORD size) {
    HANDLE file = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    DWORD written = 0;
    BOOL ok = WriteFile(file, data, size, &written, NULL);
    CloseHandle(file);
    return ok && written == size;
}

static void print_error(const char *what, const char *path) {
    DWORD code = GetLastError();
    char message[512] = "";
    FormatMessageA(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL, code, 0, message, sizeof message, NULL);
    printf("  Could not %s %s: %s", what, path, message[0] ? message : "unknown error\n");
    if (code == ERROR_SHARING_VIOLATION) printf("  NexStorm is probably still running: close it and try again.\n");
}

/* The connector: next to this program if it is there (the signed copy), otherwise the one built in. */
static const unsigned char *connector_bytes(DWORD *size, unsigned char **owned) {
    char path[MAX_PATH];
    *owned = NULL;
    if (GetModuleFileNameA(NULL, path, sizeof path)) {
        char *slash = strrchr(path, '\\');
        if (slash) {
            strcpy(slash + 1, DLL_NAME);
            unsigned char *data = read_file(path, size);
            if (data && contains(data, *size, MARKER)) {
                *owned = data;
                return data;
            }
            free(data);
        }
    }
    HRSRC resource = FindResourceA(NULL, MAKEINTRESOURCEA(CONNECTOR_RESOURCE), (LPCSTR)RT_RCDATA);
    if (!resource) return NULL;
    HGLOBAL loaded = LoadResource(NULL, resource);
    *size = SizeofResource(NULL, resource);
    return loaded ? (const unsigned char *)LockResource(loaded) : NULL;
}

/* Settings file */

static void find_settings_path(void) {
    const char *appdata = getenv("APPDATA");
    snprintf(settings_path, sizeof settings_path, "%s\\Lightning Monitor Bridge\\nexstorm-connector.ini", appdata ? appdata : ".");
}

static void load_settings(Settings *settings) {
    memset(settings, 0, sizeof *settings);
    strcpy(settings->port, "10001");
    strcpy(settings->squelch_scale, "auto");
    FILE *file = fopen(settings_path, "r");
    if (!file) return;
    char raw[1024];
    while (fgets(raw, sizeof raw, file)) {
        char *line = trim(raw);
        char *equals = strchr(line, '=');
        if (!*line || *line == ';' || *line == '#' || !equals) continue;
        *equals = '\0';
        char *key = trim(line), *value = trim(equals + 1);
        if (!strcmp(key, "host")) snprintf(settings->host, sizeof settings->host, "%s", value);
        else if (!strcmp(key, "port") && *value) snprintf(settings->port, sizeof settings->port, "%s", value);
        else if (!strcmp(key, "token")) snprintf(settings->token, sizeof settings->token, "%s", value);
        else if (!strcmp(key, "allow_settings")) settings->allow_settings = *value == '1';
        else if (!strcmp(key, "squelch_scale") && *value) snprintf(settings->squelch_scale, sizeof settings->squelch_scale, "%s", value);
    }
    fclose(file);
}

static int save_settings(const Settings *settings) {
    char folder[MAX_PATH];
    snprintf(folder, sizeof folder, "%s", settings_path);
    char *slash = strrchr(folder, '\\');
    if (slash) *slash = '\0';
    CreateDirectoryA(folder, NULL);
    FILE *file = fopen(settings_path, "w");
    if (!file) {
        printf("  Could not write %s\n", settings_path);
        return 0;
    }
    fprintf(file, "; Lightning Monitor NexStorm Connector: the shared detector NexStorm reads. Written by NexStorm Connector Setup.\n");
    fprintf(file, "host = %s\nport = %s\ntoken = %s\nallow_settings = %d\nsquelch_scale = %s\n",
            settings->host, settings->port, settings->token, settings->allow_settings, settings->squelch_scale);
    fclose(file);
    return 1;
}

/* NexStorm */

static int running_nexstorm(DWORD *pid, char *path, DWORD path_size) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32 entry;
    entry.dwSize = sizeof entry;
    int found = 0;
    for (BOOL more = Process32First(snapshot, &entry); more; more = Process32Next(snapshot, &entry)) {
        if (!_stricmp(entry.szExeFile, "NexStorm.exe")) {
            *pid = entry.th32ProcessID;
            found = 1;
            if (path) {
                path[0] = '\0';
                HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, *pid);
                if (process) {
                    DWORD size = path_size;
                    if (!QueryFullProcessImageNameA(process, 0, path, &size)) path[0] = '\0';
                    CloseHandle(process);
                }
            }
            break;
        }
    }
    CloseHandle(snapshot);
    return found;
}

static int has_nexstorm(const char *folder) {
    char path[PATH_SIZE];
    snprintf(path, sizeof path, "%s\\NexStorm.exe", folder);
    return file_exists(path);
}

static void registry_locations(HKEY root, const char *key_path, REGSAM view) {
    HKEY uninstall;
    if (nexstorm_folder[0] || RegOpenKeyExA(root, key_path, 0, KEY_READ | view, &uninstall) != ERROR_SUCCESS) return;
    char name[256];
    for (DWORD index = 0; !nexstorm_folder[0]; index++) {
        DWORD name_size = sizeof name;
        if (RegEnumKeyExA(uninstall, index, name, &name_size, NULL, NULL, NULL, NULL) != ERROR_SUCCESS) break;
        HKEY entry;
        if (RegOpenKeyExA(uninstall, name, 0, KEY_READ | view, &entry) != ERROR_SUCCESS) continue;
        char display[256] = "", location[MAX_PATH] = "";
        DWORD size = sizeof display;
        RegQueryValueExA(entry, "DisplayName", NULL, NULL, (BYTE *)display, &size);
        size = sizeof location;
        RegQueryValueExA(entry, "InstallLocation", NULL, NULL, (BYTE *)location, &size);
        RegCloseKey(entry);
        if (!_strnicmp(display, "NexStorm", 8)) {
            char *folder = trim(location);
            size_t length = strlen(folder);
            if (length && folder[length - 1] == '\\') folder[length - 1] = '\0';
            if (has_nexstorm(folder)) snprintf(nexstorm_folder, sizeof nexstorm_folder, "%s", folder);
        }
    }
    RegCloseKey(uninstall);
}

static void find_nexstorm(void) {
    if (nexstorm_folder[0]) return;
    DWORD pid;
    char path[MAX_PATH];
    if (running_nexstorm(&pid, path, sizeof path) && path[0]) {
        char *slash = strrchr(path, '\\');
        if (slash) {
            *slash = '\0';
            snprintf(nexstorm_folder, sizeof nexstorm_folder, "%s", path);
            return;
        }
    }
    const char *uninstall = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall";
    registry_locations(HKEY_LOCAL_MACHINE, uninstall, KEY_WOW64_32KEY);
    registry_locations(HKEY_LOCAL_MACHINE, uninstall, KEY_WOW64_64KEY);
    registry_locations(HKEY_CURRENT_USER, uninstall, 0);
    if (nexstorm_folder[0]) return;
    const char *candidates[] = {"C:\\Astrogenic\\NexStorm", "C:\\Program Files (x86)\\Astrogenic\\NexStorm",
                                "C:\\Program Files\\Astrogenic\\NexStorm", "C:\\NexStorm", NULL};
    for (int index = 0; candidates[index]; index++) {
        if (has_nexstorm(candidates[index])) {
            snprintf(nexstorm_folder, sizeof nexstorm_folder, "%s", candidates[index]);
            return;
        }
    }
}

static BOOL CALLBACK close_window(HWND window, LPARAM pid) {
    DWORD owner = 0;
    GetWindowThreadProcessId(window, &owner);
    if (owner == (DWORD)pid && IsWindowVisible(window) && !GetWindow(window, GW_OWNER)) PostMessageA(window, WM_CLOSE, 0, 0);
    return TRUE;
}

/* Returns 1 when NexStorm is not running (any more). */
static int close_nexstorm(void) {
    DWORD pid;
    if (!running_nexstorm(&pid, NULL, 0)) return 1;
    printf("\nNexStorm is running. It has to close while its LD350_DLL.dll is changed.\n");
    if (!ask_yes("Close NexStorm now?", 1)) return 0;
    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    EnumWindows(close_window, (LPARAM)pid);
    DWORD waited = process ? WaitForSingleObject(process, 20000) : WAIT_FAILED;
    if (process) CloseHandle(process);
    if (waited == WAIT_OBJECT_0) {
        printf("  NexStorm closed.\n");
        return 1;
    }
    printf("  NexStorm is still open (it may be asking a question). Close it");
    if (!interactive) {
        printf(", then run setup again.\n");
        return 0;
    }
    char ignored[8];
    read_line(", then press Enter.", ignored, sizeof ignored, 0);
    return !running_nexstorm(&pid, NULL, 0);
}

static void start_nexstorm(void) {
    char path[PATH_SIZE];
    snprintf(path, sizeof path, "\"%s\\NexStorm.exe\"", nexstorm_folder);
    /* Through Explorer, so NexStorm runs as the signed-in user rather than as administrator. */
    if ((INT_PTR)ShellExecuteA(NULL, "open", "explorer.exe", path, NULL, SW_SHOWNORMAL) > 32) {
        printf("  Started NexStorm.\n");
    } else {
        printf("  Could not start NexStorm; start it as usual.\n");
    }
}

/* The DLLs NexStorm might load: beside NexStorm.exe (the one it uses) and Boltek's copies in x86 and x64. */
static int targets(char paths[3][PATH_SIZE]) {
    int count = 0;
    const char *subfolders[] = {"", "\\x86", "\\x64"};
    for (int index = 0; index < 3; index++) {
        char folder[MAX_PATH + 8], backup[PATH_SIZE + 8];
        snprintf(folder, sizeof folder, "%s%s", nexstorm_folder, subfolders[index]);
        snprintf(paths[count], PATH_SIZE, "%s\\%s", folder, DLL_NAME);
        snprintf(backup, sizeof backup, "%s%s", paths[count], BACKUP_SUFFIX);
        if (index == 0 || file_exists(paths[count]) || file_exists(backup)) count++;
    }
    return count;
}

static const char *state(void) {
    char paths[3][PATH_SIZE];
    int count = targets(paths), installed = 0;
    for (int index = 0; index < count; index++) installed += is_connector(paths[index]);
    return installed == count ? "installed" : installed ? "partly installed" : "not installed";
}

static void show_status(void) {
    Settings settings;
    load_settings(&settings);
    printf("\nNexStorm:   %s\n", nexstorm_folder[0] ? nexstorm_folder : "not found");
    if (nexstorm_folder[0]) {
        printf("Connector:  %s\n", state());
        char paths[3][PATH_SIZE];
        int count = targets(paths);
        for (int index = 0; index < count; index++) {
            printf("  %s: %s\n", paths[index], is_connector(paths[index]) ? "connector" : file_exists(paths[index]) ? "Boltek's" : "none");
        }
    }
    if (settings.host[0]) {
        printf("Detector:   %s:%s%s\n", settings.host, settings.port, settings.token[0] ? " (token set)" : "");
    } else {
        printf("Detector:   not set\n");
    }
    printf("NexStorm may change the detector's settings: %s\n", settings.allow_settings ? "yes" : "no");
    printf("Settings:   %s\n", settings_path);
}

/* Connection test: the connector itself, loaded from a temporary folder, opens the detector with these settings. */
static int test_connection(void) {
    DWORD size = 0;
    unsigned char *owned;
    const unsigned char *bytes = connector_bytes(&size, &owned);
    if (!bytes) {
        printf("  The connector is missing from this program.\n");
        return 0;
    }
    char temp[MAX_PATH], folder[PATH_SIZE], dll[PATH_SIZE + 16], log[PATH_SIZE + 16];
    GetTempPathA(sizeof temp, temp);
    snprintf(folder, sizeof folder, "%sNexStormConnectorTest-%lu", temp, GetCurrentProcessId());
    CreateDirectoryA(folder, NULL);
    snprintf(dll, sizeof dll, "%s\\%s", folder, DLL_NAME);
    snprintf(log, sizeof log, "%s\\test.log", folder);
    int written = write_file(dll, bytes, size);
    free(owned);
    if (!written) {
        printf("  Could not prepare the test.\n");
        return 0;
    }
    char variable[PATH_SIZE + 40];
    SetEnvironmentVariableA("LD350_CONNECTOR_CONFIG", settings_path);
    SetEnvironmentVariableA("LD350_CONNECTOR_LOG", log);
    snprintf(variable, sizeof variable, "LD350_CONNECTOR_CONFIG=%s", settings_path);
    _putenv(variable);
    snprintf(variable, sizeof variable, "LD350_CONNECTOR_LOG=%s", log);
    _putenv(variable);

    printf("  Testing the connection (up to 10 seconds)...\n");
    int connected = 0;
    HMODULE library = LoadLibraryA(dll);
    if (library) {
        typedef int (*Call)(void);
        Call open = (Call)(void *)GetProcAddress(library, "?LD350_Open@@YAHXZ");
        Call close = (Call)(void *)GetProcAddress(library, "?LD350_Close@@YAHXZ");
        if (open && close) {
            connected = open() == 1;
            close();
        }
        FreeLibrary(library);
    }
    FILE *file = fopen(log, "r");
    if (file) {
        char line[512];
        while (fgets(line, sizeof line, file)) {
            const char *text = strlen(line) > 21 ? line + 21 : line;
            if (strstr(text, "Connected to") || strstr(text, "Could not") || strstr(text, "No detector")) printf("  %s", text);
        }
        fclose(file);
    }
    DeleteFileA(dll);
    DeleteFileA(log);
    RemoveDirectoryA(folder);
    SetEnvironmentVariableA("LD350_CONNECTOR_CONFIG", NULL);
    SetEnvironmentVariableA("LD350_CONNECTOR_LOG", NULL);
    _putenv("LD350_CONNECTOR_CONFIG=");
    _putenv("LD350_CONNECTOR_LOG=");
    printf(connected ? "  The connection works.\n" : "  The connection did not work.\n");
    return connected;
}

/* Install and restore */

static int install_files(void) {
    DWORD size = 0;
    unsigned char *owned;
    const unsigned char *bytes = connector_bytes(&size, &owned);
    if (!bytes) {
        printf("  The connector is missing from this program.\n");
        return 0;
    }
    char paths[3][PATH_SIZE];
    int count = targets(paths), ok = 1;
    for (int index = 0; index < count && ok; index++) {
        char backup[PATH_SIZE + 8];
        snprintf(backup, sizeof backup, "%s%s", paths[index], BACKUP_SUFFIX);
        if (file_exists(paths[index]) && !is_connector(paths[index])) {
            /* A Boltek DLL here (even after an earlier install, if NexStorm was reinstalled) is the one to keep. */
            if (!MoveFileExA(paths[index], backup, MOVEFILE_REPLACE_EXISTING)) {
                print_error("keep Boltek's", paths[index]);
                ok = 0;
                break;
            }
            printf("  Kept Boltek's %s as %s%s\n", paths[index], DLL_NAME, BACKUP_SUFFIX);
        }
        if (!write_file(paths[index], bytes, size)) {
            print_error("write", paths[index]);
            ok = 0;
            break;
        }
        printf("  Installed the connector as %s\n", paths[index]);
    }
    free(owned);
    return ok;
}

static int restore_files(void) {
    char paths[3][PATH_SIZE];
    int count = targets(paths), ok = 1, changed = 0;
    for (int index = 0; index < count; index++) {
        char backup[PATH_SIZE + 8];
        snprintf(backup, sizeof backup, "%s%s", paths[index], BACKUP_SUFFIX);
        if (file_exists(backup)) {
            if (MoveFileExA(backup, paths[index], MOVEFILE_REPLACE_EXISTING)) {
                printf("  Restored Boltek's %s\n", paths[index]);
                changed = 1;
            } else {
                print_error("restore", paths[index]);
                ok = 0;
            }
        } else if (is_connector(paths[index])) {
            if (DeleteFileA(paths[index])) {
                printf("  Removed the connector from %s\n", paths[index]);
                changed = 1;
            } else {
                print_error("remove", paths[index]);
                ok = 0;
            }
        }
    }
    if (ok && !changed) printf("  Boltek's driver was already in place.\n");
    return ok;
}

static int choose_detector(Settings *settings) {
    char answer[256], prompt[512];
    printf("\nWhich computer shares the detector?\n");
    printf("  The Mac running Lightning Monitor with Settings > Detector > Sharing on (its name is shown there under\n");
    printf("  \"Other devices connect to\", e.g. Mac-mini-M1.local), or its IP address. For a detector on this PC's own\n");
    printf("  USB shared by Lightning Monitor Bridge, enter 127.0.0.1.\n");
    for (;;) {
        snprintf(prompt, sizeof prompt, "Computer%s%s%s: ", settings->host[0] ? " [" : "", settings->host, settings->host[0] ? "]" : "");
        read_line(prompt, answer, sizeof answer, 0);
        if (answer[0]) snprintf(settings->host, sizeof settings->host, "%s", answer);
        if (settings->host[0] && !strpbrk(settings->host, " \t")) break;
        printf("  Enter a computer name or IP address, without spaces.\n");
    }
    snprintf(prompt, sizeof prompt, "Port [%s]: ", settings->port);
    read_line(prompt, answer, sizeof answer, 0);
    if (answer[0]) snprintf(settings->port, sizeof settings->port, "%.15s", answer);
    printf("Token: the one in Lightning Monitor's sharing settings (Settings > Detector > Sharing > Token).\n");
    read_line(settings->token[0] ? "Token [Enter keeps the saved one, - for none]: " : "Token [Enter for none]: ", answer, sizeof answer, 1);
    if (!strcmp(answer, "-")) settings->token[0] = '\0';
    else if (answer[0]) snprintf(settings->token, sizeof settings->token, "%s", answer);
    printf("NexStorm's squelch and alarm settings would change the detector for Lightning Monitor and every other viewer.\n");
    settings->allow_settings = ask_yes("Let NexStorm change the detector's settings?", settings->allow_settings);
    return 1;
}

static int install(Settings *settings, int start_after) {
    if (!save_settings(settings)) return 0;
    printf("  Saved the settings to %s\n", settings_path);
    if (!test_connection()) {
        if (!ask_yes("Install anyway?", 0)) return 0;
    }
    if (!close_nexstorm()) {
        printf("  Nothing was changed in NexStorm's folder.\n");
        return 0;
    }
    if (!install_files()) return 0;
    printf("\nDone. NexStorm reads the detector shared by %s.\n", settings->host);
    printf("If NexStorm says it can't find the receiver, the connector's log says why:\n  %s\n",
           "%APPDATA%\\Lightning Monitor Bridge\\nexstorm-connector.log");
    if (start_after && ask_yes("Start NexStorm now?", 1)) start_nexstorm();
    return 1;
}

static int restore(int start_after) {
    if (!close_nexstorm()) {
        printf("  Nothing was changed in NexStorm's folder.\n");
        return 0;
    }
    if (!restore_files()) return 0;
    printf("\nNexStorm uses Boltek's driver and this PC's USB again.\n");
    if (start_after && ask_yes("Start NexStorm now?", 1)) start_nexstorm();
    return 1;
}

static const char *option_value(const char *argument, const char *name) {
    size_t length = strlen(name);
    if ((argument[0] == '/' || argument[0] == '-') && !_strnicmp(argument + 1 + (argument[1] == '-'), name, length)) {
        const char *rest = argument + 1 + (argument[1] == '-') + length;
        if (*rest == '=' || *rest == ':') return rest + 1;
        if (!*rest) return "";
    }
    return NULL;
}

int main(int argc, char **argv) {
    printf("Lightning Monitor NexStorm Connector Setup %s\n", CONNECTOR_VERSION);
    printf("Lets NexStorm read an LD-350 or LD-250 shared by Lightning Monitor, at the same time as Lightning Monitor.\n");
    find_settings_path();
    Settings settings;
    load_settings(&settings);

    const char *action = NULL;
    int start_after = 1;
    for (int index = 1; index < argc; index++) {
        const char *value;
        if ((value = option_value(argv[index], "install"))) action = "install";
        else if ((value = option_value(argv[index], "restore"))) action = "restore";
        else if ((value = option_value(argv[index], "status"))) action = "status";
        else if ((value = option_value(argv[index], "host"))) snprintf(settings.host, sizeof settings.host, "%s", value);
        else if ((value = option_value(argv[index], "port"))) snprintf(settings.port, sizeof settings.port, "%s", value);
        else if ((value = option_value(argv[index], "token"))) snprintf(settings.token, sizeof settings.token, "%s", value);
        else if ((value = option_value(argv[index], "allow-settings"))) settings.allow_settings = 1;
        else if ((value = option_value(argv[index], "folder"))) snprintf(nexstorm_folder, sizeof nexstorm_folder, "%s", value);
        else if ((value = option_value(argv[index], "no-start"))) start_after = 0;
        else {
            printf("Unknown option %s. Options: /install /host= /port= /token= /allow-settings /folder= /no-start, /restore, /status\n", argv[index]);
            return 2;
        }
    }
    if (action) interactive = 0;

    find_nexstorm();
    while (!nexstorm_folder[0] || !has_nexstorm(nexstorm_folder)) {
        if (!interactive) {
            printf("NexStorm not found: add /folder=\"C:\\path\\to\\NexStorm\"\n");
            return 1;
        }
        printf("\nNexStorm wasn't found.\n");
        read_line("NexStorm's folder (the one with NexStorm.exe), or Enter to quit: ", nexstorm_folder, sizeof nexstorm_folder, 0);
        if (!nexstorm_folder[0]) return 1;
        if (!folder_exists(nexstorm_folder)) printf("  That folder doesn't exist.\n");
    }

    if (action) {
        if (!strcmp(action, "status")) {
            show_status();
            return 0;
        }
        if (!strcmp(action, "restore")) return restore(start_after) ? 0 : 1;
        if (!settings.host[0]) {
            printf("/install needs /host= (the computer sharing the detector)\n");
            return 1;
        }
        return install(&settings, start_after) ? 0 : 1;
    }

    for (;;) {
        show_status();
        printf("\n  1  Install or update the connector, and choose the detector\n");
        printf("  2  Test the connection\n");
        printf("  3  Restore Boltek's driver (NexStorm uses this PC's USB again)\n");
        printf("  4  Quit\n");
        char choice[16];
        read_line("Choose [1]: ", choice, sizeof choice, 0);
        if (!choice[0] || choice[0] == '1') {
            if (choose_detector(&settings)) install(&settings, 1);
            pause_before_exit();
            return 0;
        }
        if (choice[0] == '2') {
            if (!settings.host[0]) printf("  Choose the detector first (1).\n");
            else test_connection();
        } else if (choice[0] == '3') {
            restore(1);
            pause_before_exit();
            return 0;
        } else if (choice[0] == '4' || choice[0] == 'q' || choice[0] == 'Q') {
            return 0;
        }
    }
}
