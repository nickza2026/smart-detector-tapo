from multiprocessing import freeze_support

if __name__ == '__main__':
    # Must precede GUI imports: frozen multiprocessing children must never
    # interpret their private bootstrap arguments as a new desktop window.
    freeze_support()
    from smart_tapo.desktop import main
    raise SystemExit(main())
