#pragma once

#include "media/PacketQueue.hpp"
#include "media/Demuxer.hpp"
#include "media/VideoDecoder.hpp"
#include "media/VideoRenderer.hpp"
#include "media/MediaTypes.hpp"

#include <QObject>
#include <QThread>
#include <QPointer>

class MediaSession : public QObject
{
    Q_OBJECT

public:
    explicit MediaSession(QObject* parent = nullptr);
    ~MediaSession();

    bool open(const QString& url);
    void play();
    void stop();
    void setPacingStartUs(int64_t startUs);
    VideoRenderer* videoRenderer();

signals:
    void error(const QString& message);
    void failed();

private slots:
    void onLoopReset();

private:
    PacketQueue m_videoQueue;
    QThread m_demuxThread;
    QThread m_videoThread;
    QPointer<Demuxer> m_demuxer;
    QPointer<VideoDecoder> m_videoDecoder;
    VideoRenderer m_videoRenderer;
    bool m_opened = false;
    bool m_playing = false;
};
