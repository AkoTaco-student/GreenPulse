#include "monitor/h/ShopPlayer.hpp"
#include "connectors/StatnettClient.hpp"
#include "telemetry/MetadataStore.hpp"

#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>
#include <QTimer>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QDebug>
#include <QStringList>

#ifdef Q_OS_MACOS
#include <IOKit/pwr_mgt/IOPMLib.h>
#endif

static void preventDisplaySleep() {
#ifdef Q_OS_MACOS
    IOPMAssertionID assertionID = 0;
    IOPMAssertionCreateWithName(
        kIOPMAssertionTypeNoDisplaySleep, kIOPMAssertionLevelOn,
        CFSTR("greenpulse Monitor Active"), &assertionID);
#endif
}

int main(int argc, char *argv[]) {
    QGuiApplication app(argc, argv);
    app.setApplicationName("greenpulse");
    app.setApplicationDisplayName("greenpulse - Market monitor");

    preventDisplaySleep();
    qDebug() << "[lifecycle] greenpulse monitor starting at"
             << QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);

    ShopPlayer player;
    QObject::connect(&player, &ShopPlayer::error, &app, [](const QString& message) {
        qWarning() << "greenpulse media error:" << message;
    });

    // Statnett klient som mater MetadataStore
    StatnettClient statnett;
    QObject::connect(&statnett, &StatnettClient::telemetryReceived, player.metadata(), &MetadataStore::update);
    QObject::connect(&statnett, &StatnettClient::fetchError, player.metadata(), &MetadataStore::setSourceStatus);
    QObject::connect(player.metadata(), &MetadataStore::scenarioRequested,
                     &statnett, [&statnett, &player](const QString& scenarioId) {
        statnett.fetchModelledDispatchForMode(scenarioId, player.metadata()->dayAheadMode());
    });
    QObject::connect(player.metadata(), &MetadataStore::dayAheadModeRequested,
                     &statnett, [&statnett](const QString& mode, const QString& scenarioId) {
        statnett.fetchModelledDispatchForMode(scenarioId, mode);
    });

    // Reset before the first independent network request.
    player.metadata()->reset();

    // Refresh independent feeds without fabricating values when a source is unavailable.
    QTimer marketTimer;
    QObject::connect(&marketTimer, &QTimer::timeout, [&statnett, &player]() {
        /*qDebug() << "[lifecycle] scheduled market refresh; scenario="
                 << player.metadata()->activeScenario();*/
        statnett.fetchLatestPrices();
        statnett.fetchPhysicalFlows();
        statnett.fetchFrequency();
        statnett.fetchModelledDispatchForMode(
            player.metadata()->activeScenario(), player.metadata()->dayAheadMode());
    });
    marketTimer.start(60000);

    // Initial feed requests.
    //qDebug() << "[lifecycle] starting initial market, physical-flow, frequency, and CVXPY requests";
    statnett.fetchLatestPrices();
    statnett.fetchPhysicalFlows();
    statnett.fetchFrequency();
    statnett.fetchModelledDispatchForMode(
        player.metadata()->activeScenario(), player.metadata()->dayAheadMode());

    const QString appDirectory = QCoreApplication::applicationDirPath();
    QStringList configCandidates{
        QDir(appDirectory).absoluteFilePath("../Resources/config.json"),
        QDir(appDirectory).absoluteFilePath("config.json"),
        QDir(appDirectory).absoluteFilePath("../config.json"),
        QDir::current().absoluteFilePath("config.json")
    };
    QString configPath;
    for (const QString& candidate : configCandidates) {
        if (QFile::exists(candidate)) {
            configPath = candidate;
            break;
        }
    }
    if (configPath.isEmpty()) {
        //qWarning() << "No greenpulse config.json found; background video disabled";
    } else if (player.loadConfig(configPath)) {
        //qDebug().noquote() << "[media] loaded player configuration from" << configPath;
        player.play();
    } else {
        //qWarning() << "greenpulse config could not be loaded from" << configPath;
    }

    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("backend", &player);

    const QUrl qmlUrl(QStringLiteral("qrc:/greenpulse/qml/main.qml"));
    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreated, &app,
        [&player, qmlUrl](QObject *obj, const QUrl &url) {
            if (url != qmlUrl && !obj) return;
            auto *window = qobject_cast<QQuickWindow *>(obj);
            if (window) {
                window->resize(1400, 900);
                window->show();
            }
        },
        Qt::QueuedConnection);

    // Prøv ressurs først, ellers last fra disk
    if (QFile::exists(QStringLiteral(":/greenpulse/qml/main.qml"))) {
        engine.load(qmlUrl);
    } else {
        QString localQml = QDir(QCoreApplication::applicationDirPath()).absoluteFilePath("../../../core/cpp/qml/main.qml");
        if (!QFile::exists(localQml))
            localQml = QDir::current().absoluteFilePath("qml/main.qml");
        engine.load(QUrl::fromLocalFile(localQml));
    }

    if (engine.rootObjects().isEmpty()) {
        //qWarning() << "QML-lasting mislyktes. Kjører som konsoll-/arbeidertjeneste.";
    }

    return app.exec();
}
