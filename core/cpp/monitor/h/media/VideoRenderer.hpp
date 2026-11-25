#pragma once

#include "media/MediaTypes.hpp"

#include <QObject>
#include <QElapsedTimer>
#include <QMutex>
#include <QTimer>
#include <QVideoSink>
#include <cstddef>
#include <deque>
#include <QDebug>

class VideoRenderer : public QObject
{
    Q_OBJECT

public:
    explicit VideoRenderer(QObject* parent = nullptr);

    QVideoSink* sink();
    void        setSink(QVideoSink* sink);
    void        setStillImageMode(bool enabled);

    // Called by MediaSession when Demuxer signals loopReset.
    void clearForLoop();

public slots:
    void enqueue(const DecodedVideoFrame& frame);
    void clear();

private slots:
    void renderNext();
    void kickTimer();

private:
    struct QueuedFrame { QVideoFrame frame; double pts = 0.0; };

    static constexpr std::size_t kMaxQueuedFrames = 8;
    static constexpr std::size_t kStartupFrames = 2;
    static constexpr double kLateFrameDropSeconds = 0.100;

    QVideoSink  m_sink;
    QVideoSink* m_outputSink  = &m_sink;
    QTimer      m_timer;
    QMutex      m_queueMutex;

    // Local monotonic clock (no master audio-clock dependency)
    QElapsedTimer m_localClock;
    bool          m_localClockStarted = false;
    double        m_localStartPts     = 0.0;
    double        m_ptsOffset         = 0.0;
    bool          m_timelineSynced    = false;
    bool          m_stillImageMode    = false;

    std::deque<QueuedFrame> m_queue;
};
