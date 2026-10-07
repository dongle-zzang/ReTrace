"""Latest-JPEG storage and HTTP delivery, adapted from hy_test/rtsp_web.py."""

import threading


class FrameStore:
    def __init__(self):
        self.condition = threading.Condition()
        self.jpeg = None
        self.sequence = 0
        self.closed = False

    def put(self, jpeg):
        with self.condition:
            if not self.closed:
                self.jpeg = jpeg
                self.sequence += 1
                observer = getattr(self, "startup_observer", None)
                if observer is not None:
                    observer("first_mjpeg_available")
                self.condition.notify_all()

    def wait_next(self, sequence, timeout=5):
        with self.condition:
            self.condition.wait_for(
                lambda: self.closed or self.sequence != sequence, timeout=timeout,
            )
            return self.sequence, self.jpeg, self.closed

    def clear(self):
        """Invalidate a stale JPEG without closing an HTTP stream during retry."""
        with self.condition:
            self.jpeg = None
            self.sequence += 1
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


def serve_mjpeg(handler, store):
    observer = getattr(store, "startup_observer", None)
    if observer is not None:
        observer("first_http_request")
    handler.send_response(200)
    handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    # Prevent a stalled HTTP client from keeping a writer blocked indefinitely.
    handler.connection.settimeout(10)
    sequence = 0
    while True:
        next_sequence, jpeg, closed = store.wait_next(sequence)
        if closed:
            return
        if next_sequence == sequence:
            continue
        sequence = next_sequence
        if jpeg is None:
            # A clear() is a new sequence too. Advance it so an offline camera
            # waits for the next JPEG rather than spinning on the cleared frame.
            continue
        handler.wfile.write(
            f"--frame\r\nContent-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\nX-Frame-Sequence: {sequence}\r\n\r\n".encode()
        )
        handler.wfile.write(jpeg)
        handler.wfile.write(b"\r\n")
        handler.wfile.flush()
        if observer is not None:
            observer("first_http_frame_flushed")
            observer = None
