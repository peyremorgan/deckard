"""Entry point for the GUI and its bundled console helper."""
import runpy
import sys


MODULES = {'deckard.scan_job', 'deckard.report', 'deckard.diagnostics', 'deckard.askpass'}


def main():
    if sys.argv[1:] == ['--smoke-test']:
        from frozen_smoke import main as smoke_main
        smoke_main()
    elif len(sys.argv) >= 3 and sys.argv[1] == '--module':
        module = sys.argv[2]
        if module not in MODULES:
            raise SystemExit('Unknown Deckard helper module: ' + module)
        sys.argv = [module, *sys.argv[3:]]
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(line_buffering=True, write_through=True)
        runpy.run_module(module, run_name='__main__', alter_sys=True)
    else:
        from deckard.gui import main as gui_main
        raise SystemExit(gui_main())


if __name__ == '__main__':
    main()
