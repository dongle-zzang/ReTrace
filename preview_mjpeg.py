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
                self.condition.notify_all()

    def wait_next(self, sequence, timeout=5):
        with self.condition:
            self.condition.wait_for(
                lambda: self.closed or self.sequence != sequence, timeout=timeout,
            )
            return self.sequence, self.jpeg, self.closed

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


def serve_mjpeg(handler, store):
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
        if next_sequence == sequence or jpeg is None:
            continue
        sequence = next_sequence
        handler.wfile.write(
            f"--frame\r\nContent-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\nX-Frame-Sequence: {sequence}\r\n\r\n".encode()
        )
        handler.wfile.write(jpeg)
        handler.wfile.write(b"\r\n")
        handler.wfile.flush()
