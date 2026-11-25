#include "ShopPlayer.hpp"

#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>

#ifdef Q_OS_MACOS
#include <IOKit/pwr_mgt/IOPMLib.h>
#elif defined(Q_OS_WIN)
#include <windows.h>
#endif

static void preventDisplaySleep() {
#ifdef Q_OS_MACOS
  IOPMAssertionID assertionID = 0;
  IOPMAssertionCreateWithName(
      kIOPMAssertionTypeNoDisplaySleep, kIOPMAssertionLevelOn,
      CFSTR("Gusto Latino Digital Signage Active"), &assertionID);
#elif defined(Q_OS_WIN)
  SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED |
                          ES_SYSTEM_REQUIRED);
#endif
}

int main(int argc, char *argv[]) {
  QGuiApplication app(argc, argv);
  app.setApplicationName("Gusto Latino");
  app.setApplicationDisplayName("Gusto Latino");

  preventDisplaySleep();

  // Prefer bundled resources so the .app can be copied independently.
  const QString resourcesDir =
      QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(
          "../Resources");
  QString configPath = resourcesDir + "/config.json";
  if (!QFile::exists(configPath))
    configPath =
        QCoreApplication::applicationDirPath() + "/config.json";
  if (!QFile::exists(configPath))
    configPath = QCoreApplication::applicationDirPath() + "/config.json";
  if (!QFile::exists(configPath))
    configPath =
        QCoreApplication::applicationDirPath() + "/../../../config.json";
  if (!QFile::exists(configPath))
    configPath =
        QCoreApplication::applicationDirPath() + "/../../../config.json";
  if (!QFile::exists(configPath))
    configPath = "/Users/akotaco/Power Market Analysis/greenpulse/core/cpp/config.json";

  ShopPlayer player;

  if (!player.loadConfig(configPath)) {
    qCritical() << "Failed to load config from" << configPath;
    return 1;
  }

  QQmlApplicationEngine engine;
  engine.rootContext()->setContextProperty("backend", &player);

  const QUrl qmlUrl("qrc:/Shop/qml/main.qml");
  QObject::connect(
      &engine, &QQmlApplicationEngine::objectCreated, &app,
      [&](QObject *obj, const QUrl &url) {
        if (url != qmlUrl)
          return;
        if (!obj) {
          app.exit(1);
          return;
        }

        // Apply window geometry from config.
        auto *window = qobject_cast<QQuickWindow *>(obj);
        if (!window)
          return;

        if (player.isFullscreen()) {
          window->showFullScreen();
        } else {
          window->resize(player.windowWidth(), player.windowHeight());
          window->show();
        }
      },
      Qt::QueuedConnection);

  engine.load(qmlUrl);

  // Start playback after the event loop is running.
  QMetaObject::invokeMethod(&player, &ShopPlayer::play, Qt::QueuedConnection);

  return app.exec();
}
