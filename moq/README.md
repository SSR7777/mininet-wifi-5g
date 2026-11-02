# moq/ - MoQ Transport (draft-16, MoQtail compatible) on picoquic

```bash
make                               # uses ~/picoquic   (or: make PICOQUIC=/path/to/picoquic)
./moqpico pub --synthetic 50,10,900,33 --cert ~/picoquic/certs/cert.pem --key ~/picoquic/certs/key.pem
./moqpico sub --connect 127.0.0.1:4443 --log objects.csv          # in a second terminal
```

- `moqt.h` / `moqt.c`: the MoQ layer. It plugs into picoquic as the connection callback
  (`moqt_picoquic_callback`) and provides sessions, SUBSCRIBE / PUBLISH_NAMESPACE / PUBLISH_DONE,
  and objects on SUBGROUP streams or OBJECT_DATAGRAMs.
- `moqpico.c`: video publisher and subscriber. Run `./moqpico` with no arguments for all options.
- `moqtail_interop.sh`: tests against the real MoQtail relay and client, release 0.14.1.

See `../PART9_MOQ.md` for the whole Part 9.
