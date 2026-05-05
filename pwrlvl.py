import time
import json
import requests
from collections import deque
from datetime import datetime, timezone

import board, busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# ------- CONFIG -------
DEVICE_ID = "pi-power-001"

API_ENDPOINT = "http://..." # needs to be filled in

V_REF = 4.096
DIVIDER_RATIO = 2.0 # needs to be calculated according to real setup

WINDOW_SIZE = 5
SAMPLE_INTERVAL = 15  # seconds

# Voltage thresholds, needs to be tuned and tested, mock data atm
STATE_TABLE = [
    ("full",     5.05, 4.95),
    ("normal",   4.95, 4.85),
    ("low",      4.80, 4.70),
    ("critical", 4.70, 0.00),
]

# ------- ADC SETUP -------
i2c = busio.I2C(board.SCL, board.SDA)
ads = ADS.ADS1115(i2c, address=0x48)
chan = AnalogIn(ads, ADS.P0)

# ------- FILTER -------
class MovingAverage:
    def __init__(self, size):
        self.buffer = deque(maxlen=size)

    def add(self, value):
        self.buffer.append(value)
        return sum(self.buffer) / len(self.buffer)

# ------- STATE MACHINE -------
def determine_state(voltage, prev_state):
    for state, enter_v, exit_v in STATE_TABLE:
        if prev_state == state:
            if voltage >= exit_v:
                return state
        else:
            if voltage >= enter_v:
                return state
    return "critical"

# ------- MAIN -------
def main():
    print("Battery monitor (direct-to-DB) started")

    filter_v = MovingAverage(WINDOW_SIZE)
    prev_voltage = None
    state = "full"

    while True:
        try:
            raw_voltage = chan.voltage
            measured_voltage = raw_voltage * DIVIDER_RATIO
            smoothed_voltage = filter_v.add(measured_voltage)

            # Trend
            trend = None
            if prev_voltage is not None:
                trend = smoothed_voltage - prev_voltage
            prev_voltage = smoothed_voltage

            # State
            state = determine_state(smoothed_voltage, state)

            payload = {
                "device_id": DEVICE_ID,
                "metric": "battery",
                "voltage": round(smoothed_voltage, 3),
                "state": state,
                "trend": round(trend, 5) if trend is not None else None,
                "sampled_at": datetime.now(timezone.utc).isoformat(),
            }

            # Send directly to DB/API
            try:
                r = requests.post(API_ENDPOINT, json=payload, timeout=3)
                print(f"[Battery] Sent {payload} | Status {r.status_code}")
            except Exception as e:
                print("[Battery] Send failed:", e)

            # Critical handling
            if state == "critical":
                print("[Battery] CRITICAL - consider shutdown")

            time.sleep(SAMPLE_INTERVAL)

        except KeyboardInterrupt:
            print("\nBattery monitor shutting down.")
            break


if __name__ == "__main__":
    main()