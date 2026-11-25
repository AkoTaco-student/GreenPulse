#include "media/MediaSession.hpp"

extern "C" {
#include <libavutil/time.h>
}

MediaSession::MediaSession(QObject* parent)
    : QObject(parent), m_videoRenderer(this)
{
}

MediaSession::~MediaSession() { stop(); }

bool MediaSession::open(const QString& url)
{
    stop();
    if (url.isEmpty()) {
        emit error(QStringLiteral("Media URL is empty"));
        return false;
    }

    m_videoQueue.reset();
    m_demuxer = new Demuxer(&m_videoQueue);
    connect(m_demuxer, &Demuxer::loopReset,
            this, &MediaSession::onLoopReset, Qt::DirectConnection);
    connect(m_demuxer, &Demuxer::error, this, &MediaSession::error,
            Qt::QueuedConnection);
    connect(m_demuxer, &Demuxer::finished, this, &MediaSession::failed,
            Qt::QueuedConnection);

    if (!m_demuxer->open(url)) {
        delete m_demuxer;
        m_demuxer = nullptr;
        return false;
    }

    const MediaStreamInfo streamInfo = m_demuxer->streamInfo();
    m_videoRenderer.setStillImageMode(streamInfo.stillImage);
    if (streamInfo.videoStreamIndex < 0) {
        emit error(QStringLiteral("Selected media contains no video stream"));
        stop();
        return false;
    }

    m_videoDecoder = new VideoDecoder(&m_videoQueue);
    connect(m_videoDecoder, &VideoDecoder::error, this, &MediaSession::error,
            Qt::QueuedConnection);
    connect(m_videoDecoder, &VideoDecoder::frameReady, &m_videoRenderer,
            &VideoRenderer::enqueue, Qt::DirectConnection);
    if (!m_videoDecoder->open(m_demuxer->videoCodecParameters(), streamInfo.videoTimeBase)) {
        emit error(QStringLiteral("Failed to open video decoder"));
        stop();
        return false;
    }

    m_opened = true;
    return true;
}

void MediaSession::play()
{
    if (!m_opened || m_playing)
        return;
    m_playing = true;

    if (m_demuxer)
        m_demuxer->moveToThread(&m_demuxThread);
    if (m_videoDecoder)
        m_videoDecoder->moveToThread(&m_videoThread);

    if (m_demuxer) {
        connect(&m_demuxThread, &QThread::started, m_demuxer,
                &Demuxer::run, Qt::QueuedConnection);
        m_demuxThread.start();
    }
    if (m_videoDecoder) {
        connect(&m_videoThread, &QThread::started, m_videoDecoder,
                &VideoDecoder::run, Qt::QueuedConnection);
        m_videoThread.start();
    }
}

void MediaSession::stop()
{
    const bool hasAnything = m_demuxer || m_videoDecoder
        || m_demuxThread.isRunning() || m_videoThread.isRunning();
    if (!hasAnything && !m_opened && !m_playing)
        return;

    if (m_demuxer)
        m_demuxer->requestStop();
    if (m_videoDecoder)
        m_videoDecoder->requestStop();
    m_videoQueue.abort();
    m_videoRenderer.clear();
    m_demuxThread.quit();
    m_videoThread.quit();
    if (m_demuxThread.isRunning())
        m_demuxThread.wait();
    if (m_videoThread.isRunning())
        m_videoThread.wait();

    delete m_demuxer;
    m_demuxer = nullptr;
    delete m_videoDecoder;
    m_videoDecoder = nullptr;
    m_videoQueue.reset();
    m_opened = false;
    m_playing = false;
}

void MediaSession::setPacingStartUs(int64_t startUs)
{
    if (m_demuxer)
        m_demuxer->setPacingStartUs(startUs);
}

void MediaSession::onLoopReset() { m_videoRenderer.clearForLoop(); }

VideoRenderer* MediaSession::videoRenderer() { return &m_videoRenderer; }
