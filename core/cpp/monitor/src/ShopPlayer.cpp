#include "ShopPlayer.hpp"

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTimeZone>
#include <QVideoSink>
#include <QDebug>
#include <QUrl>

extern "C" {
#include <libavutil/time.h>
}

ShopPlayer::ShopPlayer(QObject *parent) : QObject(parent) {
  // Advance to the next file when the display timer fires.
  connect(&m_advanceTimer, &QTimer::timeout, this, &ShopPlayer::advance);

  // Advance on a non-recoverable session failure (corrupt file, seek failure,
  // etc.).
  connect(&m_session, &MediaSession::failed, this, &ShopPlayer::advance);

  // Surface errors from the media pipeline to QML / console.
  connect(&m_session, &MediaSession::error, this, &ShopPlayer::error);

  // 1-second clock tick for Lausanne time overlay.
  connect(&m_clockTimer, &QTimer::timeout, this, &ShopPlayer::updateClock);
  m_clockTimer.setInterval(1000);
  m_clockTimer.start();
  updateClock(); // populate immediately so QML sees a value on startup
}

bool ShopPlayer::loadConfig(const QString &path) {
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly)) {
    emit error(QStringLiteral("Cannot open config: %1").arg(path));
    return false;
  }

  const QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
  if (!doc.isObject()) {
    emit error("config.json is not a JSON object");
    return false;
  }

  const QJsonObject obj = doc.object();
  const auto intValue = [](const QJsonValue& value, int fallback) {
    if (value.isString()) {
      bool ok = false;
      const int parsed = value.toString().toInt(&ok);
      return ok ? parsed : fallback;
    }
    return value.toInt(fallback);
  };
  m_configDir = QFileInfo(path).absoluteDir().absolutePath();
  m_displaySeconds = intValue(obj.value("display_seconds"), 30);
  m_fullscreen = obj.value("fullscreen").toBool(true);
  m_windowWidth = obj.value("width").toInt(1920);
  m_windowHeight = obj.value("height").toInt(1080);

  const QJsonArray streams = obj.value("streams").toArray();
  m_streams.clear();

  // Backward-compatible single-video form used by greenpulse config.json.
  const QString videoUrl = obj.value("video_url").toString().trimmed();
  if (!videoUrl.isEmpty() && streams.isEmpty()) {
    const QUrl parsed(videoUrl);
    const bool remote = parsed.scheme() == QStringLiteral("http")
        || parsed.scheme() == QStringLiteral("https");
    const QString resolved = remote ? videoUrl
        : (QFileInfo(videoUrl).isAbsolute() ? videoUrl
            : QDir(m_configDir).absoluteFilePath(videoUrl));
    if (remote || QFileInfo::exists(resolved))
      m_streams.append({resolved, obj.value("display_seconds").toInt(0)});
    else
      emit error(QStringLiteral("Video file not found: %1").arg(resolved));
  }

  for (const QJsonValue &v : streams) {
    QString raw;
    int customSeconds = 0;

    if (v.isString()) {
      raw = v.toString();
    } else if (v.isObject()) {
      const QJsonObject sobj = v.toObject();
      raw = sobj.value("path").toString();
      if (raw.isEmpty())
        raw = sobj.value("url").toString();
      customSeconds = intValue(sobj.value("display_seconds"), 0);
    }

    if (raw.isEmpty())
      continue;

    // Resolve relative paths against the config file's directory.
    const QUrl parsed(raw);
    const bool remote = parsed.scheme() == QStringLiteral("http")
      || parsed.scheme() == QStringLiteral("https");
    const QString resolved = remote ? raw : (QFileInfo(raw).isAbsolute()
      ? raw : QDir(m_configDir).absoluteFilePath(raw));

    if (!remote && !QFileInfo::exists(resolved)) {
      // Flag missing files but keep going.
      emit error(
          QStringLiteral("Stream file not found (skipped): %1").arg(resolved));
      continue;
    }

    m_streams.append({resolved, customSeconds});
  }

  if (m_streams.isEmpty()) {
    emit error("No valid stream files found in config");
    return false;
  }

  return true;
}

void ShopPlayer::play() {
  if (m_streams.isEmpty())
    return;

  m_currentIndex = 0;
  advance(); // advance() opens index 0 then increments ready for next time
}

// Internal: stop current session, open next stream, start.
void ShopPlayer::advance() {
  m_advanceTimer.stop();
  m_session.stop();
  qDebug() << "Advancing to next stream index" << m_currentIndex;

  if (m_streams.isEmpty())
    return;

  // Wrap index — ensures infinite rotation.
  m_currentIndex = m_currentIndex % m_streams.count();
  const StreamItem item = m_streams.at(m_currentIndex);
  m_currentIndex = (m_currentIndex + 1) % m_streams.count();

  if (!m_session.open(item.url)) {
    // open() already emitted error; skip to next file immediately.
    QMetaObject::invokeMethod(this, &ShopPlayer::advance,
                              Qt::QueuedConnection);
    return;
  }

  m_session.setPacingStartUs(av_gettime_relative());
  m_session.play();

  const int duration =
      (item.displaySeconds > 0) ? item.displaySeconds : m_displaySeconds;
  if (duration > 0)
    m_advanceTimer.start(duration * 1000);
  // If display_seconds == 0 the advance timer never fires;
  // the file loops forever until manually stopped or failed.
}

void ShopPlayer::bindVideoSink(QVideoSink *sink) {
  m_session.videoRenderer()->setSink(sink);
}

void ShopPlayer::updateClock() {
  // Oslo is in timezone Europe/Oslo (CET UTC+1 / CEST UTC+2).
  static const QTimeZone oslo("Europe/Oslo");
  const QString t =
      QDateTime::currentDateTime().toTimeZone(oslo).toString("HH:mm:ss");

  if (t != m_clockString) {
    m_clockString = t;
    emit clockStringChanged();
  }
}
