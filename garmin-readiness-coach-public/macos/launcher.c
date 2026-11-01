/* Native entry point of "Readiness Coach for Garmin.app" (built by build_app.sh).
 * It embeds the project's .venv Python instead of exec'ing it, so the Dock shows this
 * bundle's name and icon (not Python's) and macOS keeps it to a single instance.
 * PROJECT_DIR is baked in at build time; rebuild if the project folder moves. */
#include <Python.h>
#include <stdio.h>
#include <sys/stat.h>

#ifndef PROJECT_DIR
#error "build with -DPROJECT_DIR=\"/path/to/project\""
#endif

#define LOG_DIR PROJECT_DIR "/logs"
#define LOG_FILE LOG_DIR "/readiness_coach.log"
#define MAX_LOG_BYTES (2 * 1024 * 1024)

static void redirect_output(void) {
    struct stat st;
    mkdir(LOG_DIR, 0755);
    if (stat(LOG_FILE, &st) == 0 && st.st_size > MAX_LOG_BYTES)
        rename(LOG_FILE, LOG_FILE ".1");  /* keep one old log */
    freopen("/dev/null", "r", stdin);
    freopen(LOG_FILE, "a", stdout);
    freopen(LOG_FILE, "a", stderr);
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;  /* ignore anything Launch Services passes */
    redirect_output();

    char *args[] = {PROJECT_DIR "/macos/app_main.py"};
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;
    config.buffered_stdio = 0;
    config.install_signal_handlers = 1;
    /* Pointing executable at .venv/bin/python makes Python find pyvenv.cfg and use the venv. */
    PyStatus status = PyConfig_SetBytesString(&config, &config.executable, PROJECT_DIR "/.venv/bin/python");
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.program_name, PROJECT_DIR "/.venv/bin/python");
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesArgv(&config, 1, args);
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.run_filename, args[0]);
    if (!PyStatus_Exception(status))
        status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "Readiness Coach: Python failed to start\n");
        Py_ExitStatusException(status);
    }
    return Py_RunMain();
}
