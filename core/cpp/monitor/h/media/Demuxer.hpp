#pragma once

#include "media/PacketQueue.hpp"
#include "media/MediaTypes.hpp"

#include <QObject>
#include <QString>
#include <QMutex>
#include <QWaitCondition>
#include <atomic>
#include <cstdint>

extern "C" {
#include <libavformat/avformat.h>
}

class Demuxer : public QObject
{
    Q_OBJECT

public:
    explicit Demuxer(PacketQueue* videoQueue, QObject* parent = nullptr);
    ~Demuxer();

    bool open(const QString& url);
    void run();
    void requestStop();

    bool abort() const noexcept
    {
        return m_abort.load(std::memory_order_acquire);
    }

    void setPacingStartUs(int64_t us)
    {
        m_externalStartUs.store(us, std::memory_order_release);
    }

    const MediaStreamInfo&    streamInfo()           const;
    const AVCodecParameters*  videoCodecParameters() const;

signals:
    void loopReset();   // emitted after EOF seek
    void finished();    // terminal 
    void error(const QString& message);

private:
    static int interruptCallback(void* opaque);
    void       pacePacket(const AVPacket* packet);

    PacketQueue*      m_videoQueue     = nullptr;
    AVFormatContext*  m_formatContext  = nullptr;
    MediaStreamInfo   m_streamInfo;

    std::atomic_bool            m_abort{false};
    std::atomic<int64_t>        m_externalStartUs{-1};
    int64_t                     m_lastVideoPacketUs = 0;

    bool     m_havePacingOrigin = false;
    int64_t  m_pacingStartUs   = 0;
    double   m_pacingStartPts  = 0.0;
};
