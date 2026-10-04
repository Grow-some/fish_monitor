from __future__ import annotations

import ctypes
import errno
import mmap
import os
import select
from types import TracebackType


V4L2_DEVICE = "/dev/video0"
V4L2_SUBDEVICE = "/dev/v4l-subdev0"
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
BYTES_PER_LINE = 1280
FRAME_BYTES = BYTES_PER_LINE * FRAME_HEIGHT
BUFFER_COUNT = 4

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_ANY = 0

V4L2_CTRL_CLASS_USER = 0x00980000
V4L2_CTRL_CLASS_IMAGE_SOURCE = 0x009E0000
V4L2_CID_BASE = V4L2_CTRL_CLASS_USER | 0x900
V4L2_CID_EXPOSURE = V4L2_CID_BASE + 17
V4L2_CID_IMAGE_SOURCE_CLASS_BASE = V4L2_CTRL_CLASS_IMAGE_SOURCE | 0x900
V4L2_CID_VBLANK = V4L2_CID_IMAGE_SOURCE_CLASS_BASE + 1
V4L2_CID_ANALOGUE_GAIN = V4L2_CID_IMAGE_SOURCE_CLASS_BASE + 3


def _fourcc(value: str) -> int:
    if len(value) != 4:
        raise ValueError("a V4L2 FourCC must contain exactly four characters")
    return sum(ord(character) << (index * 8) for index, character in enumerate(value))


V4L2_PIX_FMT_SGBRG10 = _fourcc("GB10")


class V4L2PixFormat(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("ycbcr_enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class V4L2FormatData(ctypes.Union):
    # c_uint64 preserves the 8-byte alignment used by other union members in
    # the kernel UAPI while raw_data fixes the union size at 200 bytes.
    _fields_ = [
        ("pix", V4L2PixFormat),
        ("raw_data", ctypes.c_uint8 * 200),
        ("_alignment", ctypes.c_uint64),
    ]


class V4L2Format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("fmt", V4L2FormatData)]


class V4L2RequestBuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("flags", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
    ]


class V4L2TimeCode(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("frames", ctypes.c_uint8),
        ("seconds", ctypes.c_uint8),
        ("minutes", ctypes.c_uint8),
        ("hours", ctypes.c_uint8),
        ("userbits", ctypes.c_uint8 * 4),
    ]


class TimeVal(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class V4L2BufferMemory(ctypes.Union):
    _fields_ = [
        ("offset", ctypes.c_uint32),
        ("userptr", ctypes.c_ulong),
        ("planes", ctypes.c_void_p),
        ("fd", ctypes.c_int32),
    ]


class V4L2Request(ctypes.Union):
    _fields_ = [("request_fd", ctypes.c_int32), ("reserved", ctypes.c_uint32)]


class V4L2Buffer(ctypes.Structure):
    _anonymous_ = ("request",)
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", TimeVal),
        ("timecode", V4L2TimeCode),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("m", V4L2BufferMemory),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request", V4L2Request),
    ]


class V4L2Control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, number: int, argument_type: type) -> int:
    return (
        (direction << 30)
        | (ctypes.sizeof(argument_type) << 16)
        | (ord("V") << 8)
        | number
    )


def _iow(number: int, argument_type: type) -> int:
    return _ioc(_IOC_WRITE, number, argument_type)


def _iowr(number: int, argument_type: type) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, number, argument_type)


VIDIOC_S_FMT = _iowr(5, V4L2Format)
VIDIOC_REQBUFS = _iowr(8, V4L2RequestBuffers)
VIDIOC_QUERYBUF = _iowr(9, V4L2Buffer)
VIDIOC_QBUF = _iowr(15, V4L2Buffer)
VIDIOC_DQBUF = _iowr(17, V4L2Buffer)
VIDIOC_STREAMON = _iow(18, ctypes.c_int)
VIDIOC_STREAMOFF = _iow(19, ctypes.c_int)
VIDIOC_S_CTRL = _iowr(28, V4L2Control)


def _ioctl(file_descriptor: int, request: int, argument: object) -> None:
    # fcntl is Linux-only; importing it lazily keeps decoder/unit-test imports
    # usable on development hosts that do not provide V4L2.
    import fcntl

    size = ctypes.sizeof(argument)
    data = bytearray(ctypes.string_at(ctypes.addressof(argument), size))
    while True:
        try:
            fcntl.ioctl(file_descriptor, request, data, True)
            break
        except InterruptedError:
            continue
    ctypes.memmove(ctypes.addressof(argument), bytes(data), size)


def set_sensor_controls(
    vertical_blanking: int,
    exposure: int,
    analogue_gain: int,
    device: str = V4L2_SUBDEVICE,
) -> None:
    flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    file_descriptor = os.open(device, flags)
    try:
        for control_id, value in (
            (V4L2_CID_VBLANK, vertical_blanking),
            (V4L2_CID_EXPOSURE, exposure),
            (V4L2_CID_ANALOGUE_GAIN, analogue_gain),
        ):
            _ioctl(file_descriptor, VIDIOC_S_CTRL, V4L2Control(control_id, value))
    finally:
        os.close(file_descriptor)


class MMapV4L2Capture:
    def __init__(self, device: str = V4L2_DEVICE) -> None:
        self.device = device
        self._file_descriptor: int | None = None
        self._buffers: list[mmap.mmap] = []
        self._streaming = False

    def __enter__(self) -> "MMapV4L2Capture":
        flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        self._file_descriptor = os.open(self.device, flags)
        return self

    def configure_format(self) -> None:
        file_descriptor = self._require_open()
        video_format = V4L2Format()
        video_format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        video_format.fmt.pix.width = FRAME_WIDTH
        video_format.fmt.pix.height = FRAME_HEIGHT
        video_format.fmt.pix.pixelformat = V4L2_PIX_FMT_SGBRG10
        video_format.fmt.pix.field = V4L2_FIELD_ANY
        _ioctl(file_descriptor, VIDIOC_S_FMT, video_format)

        actual = video_format.fmt.pix
        if (
            actual.width != FRAME_WIDTH
            or actual.height != FRAME_HEIGHT
            or actual.pixelformat != V4L2_PIX_FMT_SGBRG10
            or actual.bytesperline != BYTES_PER_LINE
            or actual.sizeimage < FRAME_BYTES
        ):
            actual_fourcc = bytes(
                (actual.pixelformat >> shift) & 0xFF for shift in (0, 8, 16, 24)
            ).decode("ascii", errors="replace")
            raise RuntimeError(
                "camera did not accept 640x480 GB10 with a 1280-byte stride "
                f"(got {actual.width}x{actual.height} {actual_fourcc}, "
                f"stride={actual.bytesperline}, size={actual.sizeimage})"
            )

    def start(self) -> None:
        file_descriptor = self._require_open()
        request = V4L2RequestBuffers(
            count=BUFFER_COUNT,
            type=V4L2_BUF_TYPE_VIDEO_CAPTURE,
            memory=V4L2_MEMORY_MMAP,
        )
        _ioctl(file_descriptor, VIDIOC_REQBUFS, request)
        if request.count < 2:
            raise RuntimeError(f"V4L2 provided too few mmap buffers: {request.count}")

        try:
            for index in range(request.count):
                buffer = self._new_buffer(index)
                _ioctl(file_descriptor, VIDIOC_QUERYBUF, buffer)
                mapped = mmap.mmap(
                    file_descriptor,
                    buffer.length,
                    flags=mmap.MAP_SHARED,
                    prot=mmap.PROT_READ | mmap.PROT_WRITE,
                    offset=buffer.m.offset,
                )
                self._buffers.append(mapped)
                _ioctl(file_descriptor, VIDIOC_QBUF, buffer)

            buffer_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
            _ioctl(file_descriptor, VIDIOC_STREAMON, buffer_type)
            self._streaming = True
        except Exception:
            self._release_buffers()
            raise

    def read_frame(self, timeout: float = 0.5) -> bytes | None:
        file_descriptor = self._require_open()
        if not self._streaming:
            raise RuntimeError("V4L2 capture has not been started")

        try:
            readable, _, _ = select.select([file_descriptor], [], [], timeout)
        except InterruptedError:
            return None
        if not readable:
            return None

        buffer = self._new_buffer()
        try:
            _ioctl(file_descriptor, VIDIOC_DQBUF, buffer)
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return None
            raise

        if buffer.index >= len(self._buffers):
            raise RuntimeError(f"V4L2 returned invalid buffer index {buffer.index}")

        try:
            if buffer.bytesused < FRAME_BYTES:
                raise RuntimeError(
                    f"V4L2 returned a short GB10 frame: {buffer.bytesused} bytes"
                )
            return self._buffers[buffer.index][:FRAME_BYTES]
        finally:
            _ioctl(file_descriptor, VIDIOC_QBUF, buffer)

    def close(self) -> None:
        if self._file_descriptor is None:
            return
        streamoff_error: OSError | None = None
        try:
            if self._streaming:
                buffer_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
                try:
                    _ioctl(self._file_descriptor, VIDIOC_STREAMOFF, buffer_type)
                except OSError as exc:
                    streamoff_error = exc
                finally:
                    self._streaming = False
            self._release_buffers()
        finally:
            os.close(self._file_descriptor)
            self._file_descriptor = None
        if streamoff_error is not None:
            raise streamoff_error

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _new_buffer(self, index: int = 0) -> V4L2Buffer:
        return V4L2Buffer(
            index=index,
            type=V4L2_BUF_TYPE_VIDEO_CAPTURE,
            memory=V4L2_MEMORY_MMAP,
        )

    def _require_open(self) -> int:
        if self._file_descriptor is None:
            raise RuntimeError("V4L2 device is not open")
        return self._file_descriptor

    def _release_buffers(self) -> None:
        for mapped in self._buffers:
            mapped.close()
        self._buffers.clear()

        if self._file_descriptor is not None:
            request = V4L2RequestBuffers(
                count=0,
                type=V4L2_BUF_TYPE_VIDEO_CAPTURE,
                memory=V4L2_MEMORY_MMAP,
            )
            try:
                _ioctl(self._file_descriptor, VIDIOC_REQBUFS, request)
            except OSError:
                # Closing the device releases the queue even if a driver does
                # not support an explicit count=0 release after an error.
                pass
