#include "media/VideoDecoder.hpp"

#include <QImage>
#include <QThread>
#include <QVideoFrame>
#include <algorithm>

extern "C" {
#include <libavutil/error.h>
#include <libavutil/pixfmt.h>
}

namespace {
QString ffmpegError(int err) {
  char buf[AV_ERROR_MAX_STRING_SIZE]{};
  av_strerror(err, buf, sizeof(buf));
  return QString::fromUtf8(buf);
}
} // namespace

VideoDecoder::VideoDecoder(PacketQueue *queue, QObject *parent)
    : QObject(parent), m_queue(queue) {}

VideoDecoder::~VideoDecoder() {
  requestStop();

  // ----- m_sws isolation block -----
  // Replace this block when switching to a hardware or custom scaler.
  if (m_sws) {
    sws_freeContext(m_sws);
    m_sws = nullptr;
  }
  // ---------------------------------

  if (m_codecContext)
    avcodec_free_context(&m_codecContext);
}

bool VideoDecoder::open(const AVCodecParameters *parameters,
                        AVRational timeBase) {
  if (!parameters)
    return false;

  requestStop();

  // ----- m_sws isolation block -----
  if (m_sws) {
    sws_freeContext(m_sws);
    m_sws = nullptr;
  }
  // ---------------------------------

  if (m_codecContext)
    avcodec_free_context(&m_codecContext);

  for (auto &img : m_frameBuffers)
    img = QImage();
  m_currentBuffer = 0;
  m_stop.store(false, std::memory_order_relaxed);

  const AVCodec *codec = avcodec_find_decoder(parameters->codec_id);
  if (!codec) {
    emit error("Video decoder not found");
    return false;
  }

  m_codecContext = avcodec_alloc_context3(codec);
  
  if (!m_codecContext) {
    emit error("Failed to allocate video codec context");
    return false;
  }

  if (avcodec_parameters_to_context(m_codecContext, parameters) < 0) {
    emit error("Failed to copy video codec parameters");
    avcodec_free_context(&m_codecContext);
    return false;
  }

  m_timeBase = timeBase;
  
  m_codecContext->pkt_timebase = timeBase;
  m_codecContext->thread_count = 0;
  m_codecContext->thread_type = FF_THREAD_FRAME | FF_THREAD_SLICE;

  if (avcodec_open2(m_codecContext, codec, nullptr) < 0) {
    emit error("Failed to open video decoder");
    avcodec_free_context(&m_codecContext);
    return false;
  }


  return true;
}

void VideoDecoder::flush() {
  if (m_codecContext)
    avcodec_flush_buffers(m_codecContext);
  m_currentBuffer = 0;
}

void VideoDecoder::run() {
  m_stop.store(false, std::memory_order_relaxed);
  if (!m_codecContext || !m_queue) {
    emit error("Video decoder not initialized");
    return;
  }

  AVFrame *frame = av_frame_alloc();
  if (!frame) {
    emit error("Failed to allocate video frame");
    return;
  }

  DecodedVideoFrame lastValidFrame;
  bool hasLastFrame = false;

  connect(
      this, &VideoDecoder::frameReady, this,
      [&hasLastFrame, &lastValidFrame](const DecodedVideoFrame &decoded) {
        lastValidFrame = decoded;
        hasLastFrame = true;
      },
      Qt::DirectConnection);

  while (!m_stop.load(std::memory_order_relaxed)) {
    PacketQueue::PacketPtr pkt;
    const auto result = m_queue->pop(pkt);

    if (result == PacketQueue::PopResult::Aborted)
      break;

    if (result == PacketQueue::PopResult::Reset) {
      flush();
      continue;
    }

    if (result == PacketQueue::PopResult::End) {
      avcodec_send_packet(m_codecContext, nullptr);
      drainDecoder();
      break;
    }

    if (!pkt)
      continue;

    int sendResult = avcodec_send_packet(m_codecContext, pkt.get());
    if (sendResult == AVERROR(EAGAIN)) {
      // The decoder must be drained before retrying this packet; dropping an
      // EAGAIN packet creates visible gaps, especially with reordered H.264.
      while (!m_stop.load(std::memory_order_relaxed)) {
        const int r = avcodec_receive_frame(m_codecContext, frame);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF)
          break;
        if (r < 0)
          break;
        convertAndEmit(frame);
      }
      sendResult = avcodec_send_packet(m_codecContext, pkt.get());
    }
    if (sendResult < 0)
      continue;

    while (!m_stop.load(std::memory_order_relaxed)) {
      const int r = avcodec_receive_frame(m_codecContext, frame);
      if (r == AVERROR(EAGAIN) || r == AVERROR_EOF)
        break;
      if (r < 0)
        break;
      convertAndEmit(frame);
    }
  }

  av_frame_free(&frame);
}

void VideoDecoder::drainDecoder() {
  AVFrame *frame = av_frame_alloc();
  if (!frame)
    return;

  while (!m_stop.load(std::memory_order_relaxed)) {
    const int r = avcodec_receive_frame(m_codecContext, frame);
    if (r == AVERROR_EOF || r == AVERROR(EAGAIN))
      break;
    if (r < 0)
      break;
    convertAndEmit(frame);
  }

  av_frame_free(&frame);
}

bool VideoDecoder::convertAndEmit(AVFrame *frame) {
  if (!frame || frame->width <= 0 || frame->height <= 0) {
    return false;
  }

  int w = frame->width;
  int h = frame->height;

  if (w > kMaxOutputWidth || h > kMaxOutputHeight) {
    const double scale = std::min(static_cast<double>(kMaxOutputWidth) / w,
                                  static_cast<double>(kMaxOutputHeight) / h);
    w = std::max(2, static_cast<int>(w * scale) & ~1);
    h = std::max(2, static_cast<int>(h * scale) & ~1);
  }

  // ----- m_sws isolation block -----
  // To replace the scaler: swap out sws_getCachedContext / sws_scale here.
  // The interface contract: decode frame->data/linesize → write RGBA into
  // outputImage.
  m_sws = sws_getCachedContext(m_sws, frame->width, frame->height,
                               static_cast<AVPixelFormat>(frame->format), w, h,
                               AV_PIX_FMT_RGBA, SWS_FAST_BILINEAR, nullptr,
                               nullptr, nullptr);

  if (!m_sws) {
    emit error("Failed to create/update video scaler");
    return false;
  }
  // ---------------------------------

  // Find a reusable QImage buffer from the fixed pool.
  QImage *outputImage = nullptr;
  for (std::size_t i = 0; i < kFrameBufferCount; ++i) {
    const int idx = static_cast<int>((m_currentBuffer + i) % kFrameBufferCount);
    QImage &candidate = m_frameBuffers[idx];

    const bool rightSize = (candidate.width() == w && candidate.height() == h &&
                            candidate.format() == QImage::Format_RGBA8888);

    if (!rightSize) {
      if (!candidate.isNull() && !candidate.isDetached())
        continue;
      candidate = QImage(w, h, QImage::Format_RGBA8888);
      if (candidate.isNull()) {
        emit error("Failed to allocate frame buffer");
        return false;
      }
    }

    if (!candidate.isDetached())
      continue;

    outputImage = &candidate;
    m_currentBuffer = (idx + 1) % kFrameBufferCount;
    break;
  }

  while (!outputImage && !m_stop.load(std::memory_order_relaxed)) {
    for (std::size_t i = 0; i < kFrameBufferCount; ++i) {
      // ... existing pool search ...
    }
    if (!outputImage) {
      QThread::msleep(2); // Yield briefly while renderer finishes displaying a frame
    }
  }
  if (!outputImage) return false;



  uint8_t *dstData[4] = {outputImage->bits(), nullptr, nullptr, nullptr};
  int dstLinesize[4] = {static_cast<int>(outputImage->bytesPerLine()), 0, 0, 0};

  // ----- m_sws isolation block -----
  const int scaled = sws_scale(m_sws, frame->data, frame->linesize, 0,
                               frame->height, dstData, dstLinesize);
  // ---------------------------------

  if (scaled != h) {
    emit error("Video scaling produced wrong height");
    return false;
  }

  DecodedVideoFrame decoded;
  decoded.frame = QVideoFrame(*outputImage);

  const int64_t ts = frame->best_effort_timestamp != AV_NOPTS_VALUE
                         ? frame->best_effort_timestamp
                         : frame->pts;
  decoded.ptsSeconds = (ts != AV_NOPTS_VALUE)
                           ? static_cast<double>(ts) * av_q2d(m_timeBase)
                           : 0.0;

  emit frameReady(decoded);
  return true;
}

void VideoDecoder::requestStop() {
  m_stop.store(true, std::memory_order_relaxed);
}
