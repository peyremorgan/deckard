/* Native Launch Services entry point; the application itself is Python. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char executable[PATH_MAX], project[PATH_MAX], python[PATH_MAX];
    uint32_t capacity = sizeof(executable);
    if (_NSGetExecutablePath(executable, &capacity) != 0 || !realpath(executable, project)) {
        perror("Deckard launcher path"); return 1;
    }
    /* <checkout>/Deckard.app/Contents/MacOS/Deckard */
    for (int i = 0; i < 4; i++) {
        char *slash = strrchr(project, '/');
        if (!slash) return 1;
        *slash = '\0';
    }
    if (chdir(project) != 0) { perror("Deckard project folder"); return 1; }
    if (snprintf(python, sizeof(python), "%s/.venv/bin/python", project) >= sizeof(python)) return 1;
    char *args[] = {python, "-m", "deckard", NULL};
    execv(python, args);
    perror("Deckard Python environment");
    return 1;
}
