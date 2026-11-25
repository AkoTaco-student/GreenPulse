#pragma once

#include "media/PacketQueue.hpp"
#include "media/MediaTypes.hpp"

#include <QObject>
#include <atomic>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
}

#include <QImage>
#include <array>
#include <cstddef>

class VideoDecoder : public QObject
{
    Q_OBJECT

public:
    explicit VideoDecoder(PacketQueue* queue, QObject* parent = nullptr);
    ~VideoDecoder();

    bool open(const AVCodecParameters* parameters, AVRational timeBase);
    void run();
    void requestStop();

signals:
    void frameReady(const DecodedVideoFrame& frame);
    void error(const QString& message);

private:
    void flush();
    bool convertAndEmit(AVFrame* frame);
    void drainDecoder();

    // Scaler context — isolated here for easy replacement (e.g. with
    // a hardware-accelerated path or a different pixel format target).
    SwsContext* m_sws = nullptr;

    PacketQueue*    m_queue        = nullptr;
    AVCodecContext* m_codecContext = nullptr;
    AVRational      m_timeBase{};

    static constexpr int kMaxOutputWidth  = 1920;
    static constexpr int kMaxOutputHeight = 1080;
    // Must exceed the renderer's 12-frame jitter queue plus the active sink frame.
    static constexpr std::size_t kFrameBufferCount = 12;

    std::array<QImage, kFrameBufferCount> m_frameBuffers;
    int          m_currentBuffer = 0;
    std::atomic_bool m_stop{false};
};
