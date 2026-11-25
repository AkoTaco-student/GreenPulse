#pragma once

#include "media/MediaSession.hpp"
#include "telemetry/MetadataStore.hpp"

#include <QObject>
#include <QString>
#include <QStringList>
#include <QTimer>

struct StreamItem {
    QString url;
    int displaySeconds = 0;
};

class ShopPlayer : public QObject
{
    Q_OBJECT
    Q_PROPERTY(QString clockString READ clockString NOTIFY clockStringChanged)
    Q_PROPERTY(MetadataStore* metadata READ metadata CONSTANT)

public:
    explicit ShopPlayer(QObject* parent = nullptr);

    bool loadConfig(const QString& path);
    void play();

    // Called from QML to bind the video output surface.
    Q_INVOKABLE void bindVideoSink(QVideoSink* sink);

    QString clockString() const { return m_clockString; }
    MetadataStore* metadata() { return &m_metadata; }

    // Window geometry from config (used by main.cpp)
    bool isFullscreen() const { return m_fullscreen; }
    int  windowWidth()  const { return m_windowWidth; }
    int  windowHeight() const { return m_windowHeight; }

signals:
    void clockStringChanged();
    void error(const QString& message);

private slots:
    void advance();
    void updateClock();

private:
    MediaSession m_session;
    MetadataStore m_metadata;

    QList<StreamItem> m_streams;
    int         m_currentIndex    = 0;
    int         m_displaySeconds  = 30;
    bool        m_fullscreen      = true;
    int         m_windowWidth     = 1920;
    int         m_windowHeight    = 1080;

    QTimer  m_advanceTimer;
    QTimer  m_clockTimer;
    QString m_clockString;
    QString m_configDir;    // base directory for relative stream paths
};
