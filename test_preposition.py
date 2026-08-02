import unittest
from datetime import datetime, timezone
from limits import RotorLimits
from tracker import SatelliteTracker, TleSatellite, SGP4

SAMPLE_TLE = {
    "name": "ISS (ZARYA)",
    "line1": "1 25544U 98067A   24001.50000000  .00016717  00000-0  30000-3 0  9993",
    "line2": "2 25544  51.6400 200.0000 0005000 100.0000 260.0000 15.50000000140002"
}

class TestTrackerPrepositioning(unittest.TestCase):
    def setUp(self):
        self.limits = RotorLimits("virtual_limits.json")
        self.limits.latitude = 38.7
        self.limits.longitude = -9.1
        self.limits.altitude = 50.0
        self.tracker = SatelliteTracker(self.limits, ".")
        self.sat = TleSatellite(SAMPLE_TLE)

    def test_prepositioning_fields(self):
        status = self.tracker.get_tracking_status()
        self.assertIn("is_prepositioning", status)
        self.assertIn("preposition_aos_az", status)
        self.assertIn("preposition_time_to_aos", status)
        self.assertFalse(status["is_prepositioning"])

    def test_find_next_aos(self):
        self.tracker.target = self.sat
        self.tracker._sgp4 = SGP4(self.sat)
        self.tracker._epoch_jd = self.sat.epoch_jd
        
        now = datetime.now(timezone.utc)
        now_ts = (now.timestamp() - (self.sat.epoch_jd - 2440587.5) * 86400) / 60.0
        
    def test_compute_upcoming_passes(self):
        passes = self.tracker.compute_upcoming_passes(self.sat, self.limits.latitude, self.limits.longitude)
        self.assertIsInstance(passes, list)
        for p in passes:
            self.assertIn("aos", p)
            self.assertIn("los", p)
            self.assertIn("max_el", p)
            self.assertIn("aos_az", p)
            self.assertIn("los_az", p)
            self.assertIn("duration_sec", p)
            self.assertGreater(p["max_el"], 0.0)
            self.assertLessEqual(p["max_el"], 90.0)

if __name__ == "__main__":
    unittest.main()
