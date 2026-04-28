import time
import random
import socket
import json
from collections import deque
from datetime import datetime, timezone

import board, busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# ------- CONFIG -------
# UDP Configuration (Talking to Process B on the same machine)
UDP_IP   = "127.0.0.1"
UDP_PORT = 5005
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

SHELF_ID = "demo-shelf-001"

# Calibration constants, Test 3 power curve: ADC = A * Weight^B
A_CONSTANT = 14.56
B_CONSTANT = 1.05

DEADZONE_ADC         = 150
WINDOW_SIZE          = 5
DRIFT_MARGIN         = 0.15
NEAR_ZERO_THRESHOLD  = DEADZONE_ADC * 2

SAMPLE_INTERVAL_ACTIVE = 0.5  # seconds, when system is awake
SAMPLE_INTERVAL_IDLE   = 3.0  # seconds, when system is sleeping

# Full-weight reference per sensor (grams).
# The backend sends this via the "Wake" ping.
# Default to 1 000 g until a real value arrives.
FULL_WEIGHT_G: dict[str, float] = {
    "Z1_A0": 1000.0,
    "Z1_A1": 1000.0,
    "Z1_A2": 1000.0,
}


# ------- STATE TABLE -------

# Ratio = est_grams / full_weight_g
# Each state has an ENTER threshold (ratio must rise TO here to enter)
# and an EXIT threshold (ratio must fall BELOW here to leave).

# STATE_TABLE = [
#     # (state,  enter_ratio, exit_ratio)
#     (1.00,     0.85,        0.80),
#     (0.75,     0.65,        0.60),
#     (0.50,     0.40,        0.35),
#     (0.33,     0.15,        0.10),
#     (0.00,     0.00,        0.00),
# ]

# MOCK ADC  (replace with real Adafruit ADS1x15 I2C driver)
def read_adc1(sensor_id: str) -> int:

      i2c  = busio.I2C(board.SCL, board.SDA)
      ads  = ADS.ADS1115(i2c, address=0x48)   # 0x49 for Zone 1 correct
      chan = AnalogIn(ads, ADS.P0)             # P0/P1/P2 per pin
      return chan.value

def read_adc2(sensor_id: str) -> int:
    i2c  = busio.I2C(board.SCL, board.SDA)
    ads  = ADS.ADS1115(i2c, address=0x49)   # 0x49 for Zone 1 correct
    chan = AnalogIn(ads, ADS.P0)             # P0/P1/P2 per pin
    return chan.value



#------- MOVING AVERAGE FILTER -------
class MovingAverage:
    def __init__(self, size: int):
        self.buffer = deque(maxlen=size)

    def add(self, value: float) -> float:
        self.buffer.append(value)
        return sum(self.buffer) / len(self.buffer)


# ------- CALIBRATION -------
def adc_to_grams(adc: float) -> float:
    if adc <= 0:
        return 0.0
    return (adc / A_CONSTANT) ** (1.0 / B_CONSTANT)


# ------- CONDITIONAL AUTO-TARE -------
class DriftCompensator:
    """
    Stores a per-sensor ADC baseline and re-zeroes it when the shelf appears empty AND the floor has drifted beyond DRIFT_MARGIN (15%).

    Only re-tares during empty periods so a real load is never zeroed out.
    """
    def __init__(self):
        self._baseline: float | None = None

    def correct(self, sensor_id: str, filtered_adc: float) -> float:
        if self._baseline is None:
            self._baseline = filtered_adc
            return filtered_adc

        if filtered_adc <= NEAR_ZERO_THRESHOLD and self._baseline > 0:
            drift = abs(filtered_adc - self._baseline) / self._baseline
            if drift > DRIFT_MARGIN:
                print(
                    f"[Process A] ⚠  Drift on {sensor_id}: "
                    f"{drift*100:.1f}% — re-zeroing baseline."
                )
                self._baseline = filtered_adc

        return max(0.0, filtered_adc - self._baseline)


# # ------- STATE MACHINE -------
# def determine_state(ratio: float, prev_state: float) -> float:
#     """
#     Walk STATE_TABLE from highest to lowest.
#     - If already IN a state, stay until ratio drops below exit_ratio.
#     - If NOT in a state, only enter when ratio rises above enter_ratio.
#     """
#     for state, enter_ratio, exit_ratio in STATE_TABLE:
#         if prev_state == state:
#             if ratio >= exit_ratio:
#                 return state
#         else:
#             if ratio >= enter_ratio:
#                 return state
#     return 0.00


# ------- MAIN -------
def main():
    print("Process A started")

    sensors = ["Z1_A0", "Z1_A1", "Z1_A2"]

    filters = {s: MovingAverage(WINDOW_SIZE) for s in sensors}
    compensators = {s: DriftCompensator() for s in sensors}
    # states = {s: 0.0 for s in sensors}

    active_mode = True   # wake/sleep flag

    while True:
        try:
            for i, sensor in enumerate(sensors):
                raw = read_adc1(sensor)

                # Deadzone: clamp noise floor to 0
                clean = 0 if raw < DEADZONE_ADC else raw

                # Moving average: smooth crosstalk jitter
                smoothed = filters[sensor].add(clean)

                # Drift compensation: neutralise creep
                corrected = compensators[sensor].correct(sensor, smoothed)

                # Calibrate
                est_grams = adc_to_grams(corrected)

                # Ratio + hysteretic state mapping
                full_ref  = FULL_WEIGHT_G.get(sensor, 1000.0)
                ratio     = est_grams / full_ref if full_ref > 0 else 0.0
                # new_state = determine_state(ratio, states[sensor])
                # states[sensor] = new_state

                # UDP payload
                payload = {
                    "shelf_id":   SHELF_ID,
                    "scale_index": i, # to keep track of what scale/item
                    "est_grams":  round(est_grams, 2),
                    # "state":      new_state,
                    "sampled_at": datetime.now(timezone.utc).isoformat(), # should Process A handle this or Process B? 
                }

                sock.sendto(json.dumps(payload).encode(), (UDP_IP, UDP_PORT))
                print(f"[Process A] {payload}")

            interval = SAMPLE_INTERVAL_ACTIVE if active_mode else SAMPLE_INTERVAL_IDLE
            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nProcess A: shutting down.")
            break


if __name__ == "__main__":
    main()