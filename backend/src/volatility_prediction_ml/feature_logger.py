import csv
import time

class FeatureLogger:
    def __init__(self, filename="features.csv"):
        self.file = open(filename,"w",newline="")
        self.writer = csv.writer(self.file)

        self.writer.writerow([
            "timestamp",
            "mid",
            "spread",
            "spread_change",
            "imbalance",
            "trade_count"
        ])

    def log(self, ts, mid, spread, spread_change, imbalance, trade_count):
        self.writer.writerow([
            ts, mid, spread, spread_change, imbalance, trade_count
        ])
        self.file.flush() # method used to force the write buffer to be written to the underlying storage without closing the file