try:
    from annealage_pod.esp32 import boot as _boot
    _boot.up()
except Exception as _exc:
    print("main: annealage_pod.esp32.boot.up() raised {!r}".format(_exc))
