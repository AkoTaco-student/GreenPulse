#pragma once

#include <QObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QUrl>
#include <QDate>
#include <QDateTime>
#include <QTimeZone>
#include <QString>
#include "../telemetry/GridTelemetry.hpp"

class StatnettClient : public QObject
{
    Q_OBJECT

public:
    explicit StatnettClient(QObject* parent = nullptr);
    void fetchLatestPrices();
    void fetchPhysicalFlows();
    void fetchFrequency();
    void fetchModelledDispatch(const QString& scenarioId = QStringLiteral("normal_base")); // Backward-compatible REPLAY entry point.
    void fetchModelledDispatchForMode(const QString& scenarioId, const QString& mode);
    void fetchModelledDispatchForDay(const QDate& deliveryDay, const QString& scenarioId = QStringLiteral("normal_base")); // REPLAY wrapper.

signals:
    void telemetryReceived(const GridTelemetry& telemetry);
    void rawDataReceived(const QString& endpoint, const QByteArray& data);
    void fetchError(const QString& error);

private:
    void onPricesReply(QNetworkReply* reply);
    void onFlowsReply(QNetworkReply* reply);
    void onFrequencyReply(QNetworkReply* reply);
    void onDayAheadSubmitReply(QNetworkReply* reply, const QString& scenarioId, const QString& mode, const QDate& deliveryDay);
    void pollDayAheadForecastJob(const QString& jobId, const QString& scenarioId, const QString& mode, const QDate& deliveryDay, int pollCount = 0);
    void onDayAheadJobReply(QNetworkReply* reply, const QString& jobId, const QString& scenarioId, const QString& mode, const QDate& deliveryDay, int pollCount);
    void acceptDayAheadForecastResult(const QJsonObject& result, const QString& scenarioId);
    void startDayAheadForecast(const QDate& deliveryDay, const QString& scenarioId, const QString& mode);
    void reportFailure(QNetworkReply* reply, const QString& endpoint);
    QUrl configuredUrl(const char* environmentName, const QString& defaultUrl) const;

    QNetworkAccessManager m_network;
    GridTelemetry m_latestTelemetry;
    QUrl m_apiBaseUrl;
    bool m_dayAheadRequestActive = false;
    QDate m_lastDayAheadAttemptDay;
    QString m_lastDayAheadAttemptScenario;
    QString m_lastDayAheadAttemptMode;
    QString m_currentDayAheadScenario = QStringLiteral("normal_base");
    QString m_currentDayAheadMode = QStringLiteral("REPLAY");
    QDateTime m_lastDayAheadAttemptUtc;
    QDateTime m_dayAheadJobStartedUtc;
};
