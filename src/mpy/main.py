try:
    from annealage_pod import boot as _boot
    _boot.up()
except Exception as _exc:
    print("main: annealage_pod.boot.up() raised {!r}".format(_exc))
