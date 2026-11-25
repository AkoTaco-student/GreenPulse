#include "media/Demuxer.hpp"

#include <QDebug>
#include <QUrl>
#include <cstring>
#include <limits>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/dict.h>
#include <libavutil/error.h>
#include <libavutil/time.h>
}

namespace {

QString ffmpegError(int err) {
  char buf[AV_ERROR_MAX_STRING_SIZE]{};
  av_strerror(err, buf, sizeof(buf));
  return QString::fromUtf8(buf);
}

} // namespace

Demuxer::Demuxer(PacketQueue *videoQueue, QObject *parent)
  : QObject(parent), m_videoQueue(videoQueue) {}

Demuxer::~Demuxer() {
  requestStop();
  if (m_formatContext)
    avformat_close_input(&m_formatContext);
}

int Demuxer::interruptCallback(void *opaque) {
  auto *self = static_cast<Demuxer *>(opaque);
  if (!self)
    return 1;
  if (self->abort())
    return 1;

  const int64_t nowUs = av_gettime_relative();
  const int64_t lastUs = self->m_lastVideoPacketUs;
  return lastUs > 0 && nowUs - lastUs >= 5000000 ? 1 : 0;
}

bool Demuxer::open(const QString &url) {
  const QUrl parsedUrl(url);
  //qDebug().noquote() << "[media:open] host=" << parsedUrl.host()
   //                  << "path=" << parsedUrl.path();
  m_abort.store(false, std::memory_order_release);
  m_havePacingOrigin = false;
  m_pacingStartUs = 0;
  m_pacingStartPts = 0.0;
  m_streamInfo = {};
  m_lastVideoPacketUs = av_gettime_relative();

  if (m_formatContext)
    avformat_close_input(&m_formatContext);

  m_formatContext = avformat_alloc_context();
  if (!m_formatContext) {
    emit error("Failed to allocate AVFormatContext");
    return false;
  }

  m_formatContext->interrupt_callback.callback = &Demuxer::interruptCallback;
  m_formatContext->interrupt_callback.opaque = this;

  AVDictionary *opts = nullptr;
  //av_dict_set(&opts, "flags", "low_delay", 0);
  av_dict_set(&opts, "max_delay", "700000", 0);
  av_dict_set(&opts, "rw_timeout", "1000000", 0);
  av_dict_set(&opts, "probesize", "10000000", 0);
  av_dict_set(&opts, "analyzeduration", "20000000", 0);

  const QByteArray encodedUrl = url.toUtf8();
  const int ret = avformat_open_input(&m_formatContext, encodedUrl.constData(),
                                      nullptr, &opts);
  av_dict_free(&opts);

  if (ret < 0) {
    qWarning().noquote() << "[media:open] avformat_open_input failed:"
               << ffmpegError(ret);
    emit error(
        QStringLiteral("avformat_open_input failed: %1").arg(ffmpegError(ret)));
    avformat_close_input(&m_formatContext);
    return false;
  }
  /*
  if (avformat_find_stream_info(m_formatContext, nullptr) < 0) {
    emit error("avformat_find_stream_info failed");
    avformat_close_input(&m_formatContext);
    return false;
  } */

  const int vi = av_find_best_stream(m_formatContext, AVMEDIA_TYPE_VIDEO, -1,
                                     -1, nullptr, 0);
  if (vi >= 0) {
    AVStream *s = m_formatContext->streams[vi];
    m_streamInfo.videoStreamIndex = vi;
    m_streamInfo.videoTimeBase = s->time_base;
    m_streamInfo.width = s->codecpar->width;
    m_streamInfo.height = s->codecpar->height;
    const char* formatName = m_formatContext->iformat ? m_formatContext->iformat->name : nullptr;
    m_streamInfo.stillImage = formatName
      && (std::strncmp(formatName, "image", 5) == 0
        || std::strstr(formatName, "_pipe") != nullptr);
  }

  if (vi < 0) {
    //qWarning() << "[media:open] no video stream found";
    emit error("No usable video stream");
    avformat_close_input(&m_formatContext);
    return false;
  }

  /*qDebug() << "[media:open] video stream index=" << vi
           << "size=" << m_streamInfo.width << "x" << m_streamInfo.height
           << "time_base=" << m_streamInfo.videoTimeBase.num << "/"
           << m_streamInfo.videoTimeBase.den
           << "still_image=" << m_streamInfo.stillImage;
*/
  return true;
}

void Demuxer::run() {
  if (!m_formatContext) {
    emit error("Demuxer::run called without open format context");
    emit finished();
    return;
  }

  AVPacket *pkt = av_packet_alloc();
  if (!pkt) {
    emit error("Failed to allocate packet");
    emit finished();
    return;
  }

  const bool isImage = m_streamInfo.stillImage;
  //qDebug() << "[media:demux] run started; still_image=" << isImage;

  while (!abort()) {
    const int ret = av_read_frame(m_formatContext, pkt);

    if (ret == AVERROR_EOF) {
      //qDebug() << "[media:demux] EOF reached; still_image=" << isImage;

      if (isImage) {
        while (!abort()) {
          av_usleep(100000);
        }
        break;
      }
      // --- Seek back to start and signal decoders to flush ---
      const int seekRet = avformat_seek_file(
          m_formatContext, -1, std::numeric_limits<int64_t>::min(), 0,
          std::numeric_limits<int64_t>::max(), AVSEEK_FLAG_BACKWARD);

      if (seekRet < 0) {
        //qWarning().noquote() << "[media:demux] loop seek failed:" << ffmpegError(seekRet);
        emit error(
            QStringLiteral("Loop seek failed: %1").arg(ffmpegError(seekRet)));
        break;
      }

      // Tell decoders to flush old data; reset pacing origin for next
      // iteration.
      m_videoQueue->resetForLoop();
      m_havePacingOrigin = false;
      m_lastVideoPacketUs = av_gettime_relative();
      //qDebug() << "[media:demux] loop seek succeeded; packet queue and pacing reset";

      // Notify VideoRenderer to clear its local clock state.
      emit loopReset();
      continue;
    }

    if (ret < 0) {
      if (!abort()) {
        /*qWarning().noquote() << "[media:demux] read failed; possible timeout/stall, last_video_packet_age_ms="
                             << (av_gettime_relative() - m_lastVideoPacketUs) / 1000
                             << "code="
                             << ret << "detail=" << ffmpegError(ret);*/
        emit error(
            QStringLiteral("av_read_frame failed (%1): %2. Forsøker gjenoppretting…")
                .arg(ret).arg(ffmpegError(ret)));
        
        // P7 Watchdog: forsøk å re-seil til start for å unngå at spilleren dør permanent ved I-frame stall
        const int recoverySeek = avformat_seek_file(
            m_formatContext, -1, std::numeric_limits<int64_t>::min(), 0,
            std::numeric_limits<int64_t>::max(), AVSEEK_FLAG_BACKWARD);
        if (recoverySeek >= 0) {
          m_videoQueue->resetForLoop();
          m_havePacingOrigin = false;
          m_lastVideoPacketUs = av_gettime_relative();
          //qDebug() << "[media:demux] stall recovery seek succeeded; restarting from beginning";
          emit loopReset();
          av_usleep(100000); // 100ms pause før retry
          continue;
        }
        //qWarning().noquote() << "[media:demux] stall recovery seek failed:"
                             //<< ffmpegError(recoverySeek);
      }
      break;
    }

    pacePacket(pkt);

    if (!abort()) {
      if (pkt->stream_index == m_streamInfo.videoStreamIndex) {
        m_lastVideoPacketUs = av_gettime_relative();
        m_videoQueue->push(pkt);
      }
    }

    av_packet_unref(pkt);
  }

  av_packet_free(&pkt);

  if (!abort())
    emit finished();
}

void Demuxer::pacePacket(const AVPacket *pkt) {
  if (!pkt || !m_formatContext || abort())
    return;
  if (pkt->stream_index < 0 ||
      pkt->stream_index >= static_cast<int>(m_formatContext->nb_streams))
    return;

  const AVStream *stream = m_formatContext->streams[pkt->stream_index];
  // Decode/pacing order follows DTS for reordered codecs such as H.264.
  // Presentation order remains based on best_effort_timestamp in VideoDecoder.
  const int64_t ts = pkt->dts != AV_NOPTS_VALUE ? pkt->dts : pkt->pts;
  if (ts == AV_NOPTS_VALUE)
    return;

  const double ptsSeconds = ts * av_q2d(stream->time_base);

  if (!m_havePacingOrigin) {
    m_havePacingOrigin = true;
    const int64_t ext =
        m_externalStartUs.exchange(-1, std::memory_order_acq_rel);
    m_pacingStartUs = ext >= 0 ? ext : av_gettime_relative();
    m_pacingStartPts = ptsSeconds;
    return;
  }

  const int64_t targetUs =
      m_pacingStartUs +
      static_cast<int64_t>((ptsSeconds - m_pacingStartPts) * 1000000.0);

  while (!abort()) {
    const int64_t remaining = targetUs - av_gettime_relative();
    if (remaining <= 0)
      break;
    av_usleep(static_cast<unsigned int>(qMin<int64_t>(remaining, 5000)));
  }
}

void Demuxer::requestStop() {
  m_abort.store(true, std::memory_order_release);
  if (m_videoQueue)
    m_videoQueue->abort();
}

const MediaStreamInfo &Demuxer::streamInfo() const { return m_streamInfo; }

const AVCodecParameters *Demuxer::videoCodecParameters() const {
  if (!m_formatContext || m_streamInfo.videoStreamIndex < 0)
    return nullptr;
  return m_formatContext->streams[m_streamInfo.videoStreamIndex]->codecpar;
}

