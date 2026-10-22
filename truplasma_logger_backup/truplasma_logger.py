"""TruPlasma Bipolar G2.1 read-only logger.

Polls measurement channels over RS232 and appends one CSV row per interval.
Only read commands are ever sent (6112/6122/6142/6152); 6040 and every set
command are refused, so this script cannot switch the power supply on or
change any setting.

Usage:
    python truplasma_logger.py                      # COM8, 1 s interval, default channels
    python truplasma_logger.py --interval 0.5 --out run01.csv
    python truplasma_logger.py --duration 10        # stop after 10 s
Stop with Ctrl+C.
"""
import argparse
import csv
import datetime as dt
import struct
import sys
import time

import serial

READ_BYTE, READ_INT, READ_FLOAT, READ_DWORD = 0x6112, 0x6122, 0x6142, 0x6152
ALLOWED = {READ_BYTE, READ_INT, READ_FLOAT, READ_DWORD}

# (command, channel, column name). Edit this list to log more or fewer values.
CHANNELS = [
    (READ_FLOAT, 966, "Uavg_A_V"),
    (READ_FLOAT, 967, "Iavg_A_A"),
    (READ_FLOAT, 980, "Pavg_A_kW"),
    (READ_FLOAT, 983, "Ucath_A_V"),
    (READ_FLOAT, 968, "Icath_A_A"),
    (READ_FLOAT, 976, "Uavg_B_V"),
    (READ_FLOAT, 977, "Iavg_B_A"),
    (READ_FLOAT, 981, "Pavg_B_kW"),
    (READ_FLOAT, 984, "Ucath_B_V"),
    (READ_FLOAT, 978, "Icath_B_A"),
    (READ_FLOAT, 51140, "Freq_Hz"),
    (READ_FLOAT, 51141, "Duty_pct"),
    (READ_FLOAT, 233, "MicroArcRate_per_s"),
    (READ_FLOAT, 234, "HardArcRate_per_s"),
    (READ_INT, 235, "ArcCnt_Imax_A"),
    (READ_INT, 236, "ArcCnt_UxI_A"),
    (READ_INT, 237, "ArcCnt_dU_A"),
    (READ_INT, 238, "ArcCnt_Usag_A"),
    (READ_FLOAT, 932, "T_OutputCores_C"),
]


def build_frame(cmd, channel, address=0xFFFF):
    if cmd not in ALLOWED:
        raise ValueError(f"refusing to send non-read command {cmd:#06x}")
    body = bytes([address >> 8, address & 0xFF, 0x00, 0x00,
                  cmd >> 8, cmd & 0xFF, channel >> 8, channel & 0xFF])
    crc = sum(body)
    frame = bytes([len(body) + 4, 0xFF - (len(body) + 4)]) + body + bytes([crc >> 8 & 0xFF, crc & 0xFF])
    return frame


def query(port, cmd, channel, address):
    port.reset_input_buffer()
    port.write(build_frame(cmd, channel, address))
    head = port.read(2)
    if len(head) < 2 or head[1] != 0xFF - head[0]:
        raise IOError(f"ch {channel}: no/bad reply header {head.hex(' ')}")
    rest = port.read(head[0] - 2)
    reply = head + rest
    if len(reply) != head[0]:
        raise IOError(f"ch {channel}: short reply {reply.hex(' ')}")
    if sum(reply[2:-2]) & 0xFFFF != (reply[-2] << 8 | reply[-1]):
        raise IOError(f"ch {channel}: checksum error {reply.hex(' ')}")
    ack = reply[6] << 8 | reply[7]
    if ack != 0x4000:
        raise IOError(f"ch {channel}: ack {ack:04X}")
    data = reply[12:-2]
    # Verified 2026-10-22: floats are little-endian, integers big-endian.
    if cmd == READ_FLOAT:
        return round(struct.unpack("<f", data)[0], 4)
    if cmd == READ_BYTE:
        return data[0]
    return int.from_bytes(data, "big")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--address", type=lambda s: int(s, 0), default=0xFFFF)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between rows (default 1.0)")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    ap.add_argument("--out", default=None, help="CSV path (default truplasma_YYYYmmdd_HHMMSS.csv)")
    args = ap.parse_args()

    out = args.out or dt.datetime.now().strftime("truplasma_%Y%m%d_%H%M%S.csv")
    with serial.Serial(args.port, args.baud, timeout=0.3, dsrdtr=False, rtscts=False) as port, \
            open(out, "w", newline="", encoding="utf-8") as f:
        port.dtr = False
        port.rts = False
        writer = csv.writer(f)
        writer.writerow(["timestamp", "elapsed_s"] + [name for _, _, name in CHANNELS] + ["errors"])
        start = time.monotonic()
        next_t = start
        print(f"Logging {len(CHANNELS)} channels from {args.port} every {args.interval}s to {out}  (Ctrl+C to stop)")
        try:
            while True:
                now = time.monotonic()
                if args.duration and now - start >= args.duration:
                    break
                stamp = dt.datetime.now().isoformat(timespec="milliseconds")
                values, errors = [], []
                for cmd, ch, name in CHANNELS:
                    try:
                        values.append(query(port, cmd, ch, args.address))
                    except IOError as e:
                        values.append("")
                        errors.append(str(e))
                writer.writerow([stamp, round(now - start, 3)] + values + ["; ".join(errors)])
                f.flush()
                print(f"{stamp}  U_A={values[0]} V  I_A={values[1]} A  P_A={values[2]} kW"
                      + (f"  ERR: {errors[0]}" if errors else ""))
                next_t += args.interval
                lag = next_t - time.monotonic()
                if lag > 0:
                    time.sleep(lag)
                else:
                    next_t = time.monotonic()  # cycle slower than interval; don't try to catch up
        except KeyboardInterrupt:
            pass
    print(f"Saved {out}")


if __name__ == "__main__":
    sys.exit(main())
