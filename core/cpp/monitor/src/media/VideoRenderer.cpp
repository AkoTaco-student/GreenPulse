#include "media/VideoRenderer.hpp"
#include <QDebug>

VideoRenderer::VideoRenderer(QObject* parent)
    : QObject(parent),
      m_sink(this),
      m_timer(this),
      m_outputSink(&m_sink)
{
    m_timer.setSingleShot(true);
    m_timer.setTimerType(Qt::PreciseTimer);
    connect(&m_timer, &QTimer::timeout, this, &VideoRenderer::renderNext);
}

QVideoSink* VideoRenderer::sink() { return &m_sink; }

void VideoRenderer::setSink(QVideoSink* sink)
{
    m_outputSink = sink ? sink : &m_sink;
}

void VideoRenderer::setStillImageMode(bool enabled)
{
    QMutexLocker locker(&m_queueMutex);
    m_stillImageMode = enabled;
}

void VideoRenderer::enqueue(const DecodedVideoFrame& decoded)
{
    {
        QMutexLocker locker(&m_queueMutex);

        m_queue.push_back({ decoded.frame, decoded.ptsSeconds });

        while (m_queue.size() > kMaxQueuedFrames)
            m_queue.pop_front();
    }

    QMetaObject::invokeMethod(this, &VideoRenderer::kickTimer, Qt::QueuedConnection);
}

// Called by MediaSession when Demuxer emits loopReset.
// Resets the local clock so the next loop's PTS 0 is treated as a fresh start.
void VideoRenderer::clearForLoop()
{
    QMutexLocker locker(&m_queueMutex);
    // Called synchronously from the demux thread before it reads the next loop.
    // Do not touch the main-thread QTimer here.
    m_queue.clear();
    m_localClockStarted = false;
    m_localClock.invalidate();
    m_ptsOffset      = 0.0;
    m_timelineSynced = false;
}

void VideoRenderer::clear()
{
    QMutexLocker locker(&m_queueMutex);
    m_timer.stop();
    m_queue.clear();
    m_localClockStarted = false;
    m_localClock.invalidate();
    m_ptsOffset      = 0.0;
    m_timelineSynced = false;
}

void VideoRenderer::kickTimer()
{
    if (!m_timer.isActive())
        m_timer.start(0);
}

void VideoRenderer::renderNext()
{
    QVideoFrame toPresent;
    bool        due    = false;
    int         nextMs = -1;

    {
        QMutexLocker locker(&m_queueMutex);

        if (m_queue.empty()) return;

        const std::size_t startupFrames = m_stillImageMode ? 1 : kStartupFrames;
        // Do not start playback time while accumulating the startup buffer.
        if (!m_timelineSynced && m_queue.size() < startupFrames)
        {
            m_timer.start(10);
            return;
        }

        if (!m_timelineSynced)
        {
            m_localStartPts = m_queue.front().pts;
            m_ptsOffset      = -m_localStartPts;
            m_localClock.start();
            m_localClockStarted = true;
            m_timelineSynced = true;
        }

        const double master = m_localClock.elapsed() / 1000.0;

        // Drop frames that are more than 100 ms in the past.
        while (!m_queue.empty() &&
               (m_queue.front().pts + m_ptsOffset - master) < -kLateFrameDropSeconds)
        {
            m_queue.pop_front();
        }

        if (m_queue.empty()) return;

        const double delta = m_queue.front().pts + m_ptsOffset - master;

        if (delta <= 0.025)
        {
            toPresent = std::move(m_queue.front().frame);
            m_queue.pop_front();
            due = true;
        }

        if (!m_queue.empty())
        {
            const double untilNext =
                m_queue.front().pts + m_ptsOffset - master - 0.025;
            nextMs = qBound(1, static_cast<int>(untilNext * 1000.0), 15);
        }
    }

    if (due && m_outputSink)
        m_outputSink->setVideoFrame(toPresent);

    if (nextMs >= 0)
        m_timer.start(nextMs);
}
