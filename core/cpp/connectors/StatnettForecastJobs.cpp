#include "StatnettClient.hpp"

#include <QDateTime>
#include <QTimeZone>
#include <QDebug>
#include <QTimer>
#include <QJsonDocument>
#include <QJsonValue>
#include <QNetworkRequest>
#include <QVariantMap>
#include <QSet>
#include <QMap>
#include <QJsonArray>
#include <cmath>
#include <limits>


void StatnettClient::fetchModelledDispatch(const QString& scenarioId)
{
    fetchModelledDispatchForMode(scenarioId, QStringLiteral("REPLAY"));
}

void StatnettClient::fetchModelledDispatchForDay(const QDate& deliveryDay, const QString& scenarioId)
{
    startDayAheadForecast(deliveryDay, scenarioId, QStringLiteral("REPLAY"));
}

void StatnettClient::fetchModelledDispatchForMode(const QString& scenarioId, const QString& requestedMode)
{
    const QString mode = requestedMode.trimmed().toUpper();
    if (mode != QStringLiteral("REPLAY") && mode != QStringLiteral("FORECAST")) {
        emit fetchError(QStringLiteral("Unsupported day-ahead calculation mode: %1").arg(requestedMode));
        return;
    }

    // A changed mode/scenario is a new calculation key. Clear the 10-minute
    // duplicate-submit guard so switching back to a previously selected key
    // can rerun immediately after the old job has been abandoned.
    if (scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode)
        m_lastDayAheadAttemptUtc = QDateTime();
    m_currentDayAheadScenario = scenarioId;
    m_currentDayAheadMode = mode;

    const QDateTime marketStart = QDateTime::fromString(
        m_latestTelemetry.observedPriceDayStartUtc, Qt::ISODateWithMs);
    const QTimeZone oslo(QByteArrayLiteral("Europe/Oslo"));
    // Start independently of the market-price reply. The chart date is preferred;
    // on cold start, today's Oslo date avoids waiting for the 60-second feed timer.
    const QDate deliveryDate = marketStart.isValid()
        ? marketStart.toTimeZone(oslo).date()
        : QDateTime::currentDateTimeUtc().toTimeZone(oslo).date();
    startDayAheadForecast(deliveryDate, scenarioId, mode);
}

void StatnettClient::startDayAheadForecast(
    const QDate& deliveryDay, const QString& scenarioId, const QString& mode)
{
    m_currentDayAheadScenario = scenarioId;
    m_currentDayAheadMode = mode;
    if (!deliveryDay.isValid()) {
        emit fetchError(QStringLiteral("Invalid Oslo delivery date for day-ahead calculation."));
        return;
    }
    if (m_dayAheadRequestActive)
        return;

    if (m_lastDayAheadAttemptDay == deliveryDay
        && m_lastDayAheadAttemptScenario == scenarioId
        && m_lastDayAheadAttemptMode == mode
        && m_lastDayAheadAttemptUtc.isValid()
        && m_lastDayAheadAttemptUtc.secsTo(QDateTime::currentDateTimeUtc()) < 600) {
        return;
    }

    QUrl url = m_apiBaseUrl;
    url.setPath(QStringLiteral("/api/optimization/day-ahead-forecast"));
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Accept", "application/json");
    request.setTransferTimeout(20000);
    const QJsonObject body{
        {QStringLiteral("delivery_day"), deliveryDay.toString(Qt::ISODate)},
        {QStringLiteral("mode"), mode},
        {QStringLiteral("scenario_id"), scenarioId}
    };
    const QByteArray requestBody = QJsonDocument(body).toJson(QJsonDocument::Compact);
    qDebug().noquote() << "[cvxpy:day-ahead-request] POST" << url.toString(QUrl::RemoveQuery)
                       << "delivery_day=" << deliveryDay.toString(Qt::ISODate)
                       << "mode=" << mode << "scenario=" << scenarioId;
    m_dayAheadRequestActive = true;
    m_lastDayAheadAttemptDay = deliveryDay;
    m_lastDayAheadAttemptScenario = scenarioId;
    m_lastDayAheadAttemptMode = mode;
    m_lastDayAheadAttemptUtc = QDateTime::currentDateTimeUtc();
    QNetworkReply* reply = m_network.post(request, requestBody);
    connect(reply, &QNetworkReply::finished, this, [this, reply, scenarioId, mode, deliveryDay]() {
        onDayAheadSubmitReply(reply, scenarioId, mode, deliveryDay);
    });
}

void StatnettClient::onDayAheadSubmitReply(QNetworkReply* reply, const QString& scenarioId, const QString& mode, const QDate& deliveryDay)
{
    if (reply->error() != QNetworkReply::NoError) {
        const bool superseded = scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode;
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        m_lastDayAheadAttemptUtc = QDateTime();
        if (superseded) {
            reply->deleteLater();
            startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        } else {
            reportFailure(reply, QStringLiteral("greenpulse day-ahead forecast API"));
        }
        return;
    }
    const QByteArray data = reply->readAll();
    const QJsonDocument document = QJsonDocument::fromJson(data);
    reply->deleteLater();
    if (!document.isObject()) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        m_lastDayAheadAttemptUtc = QDateTime();
        emit fetchError(QStringLiteral("Day-ahead forecast API returned invalid JSON."));
        if ((scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode))
            startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        return;
    }
    if ((scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode)) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        qDebug().noquote() << "[cvxpy:day-ahead-submit] ignored stale scenario"
                           << "received=" << scenarioId << "active=" << m_currentDayAheadScenario << " mode=" << mode << " active_mode=" << m_currentDayAheadMode;
        startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        return;
    }
    const QString status = document.object().value(QStringLiteral("status")).toString();
    if (status == QStringLiteral("WAITING_FOR_FORECAST_ORIGIN")) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        m_latestTelemetry.sourceStatus = QStringLiteral("Forecast waiting for information cutoff %1")
            .arg(document.object().value(QStringLiteral("forecast_origin_utc")).toString());
        emit telemetryReceived(m_latestTelemetry);
        return;
    }
    const QString jobId = document.object().value(QStringLiteral("job_id")).toString();
    if (jobId.isEmpty()) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        emit fetchError(QStringLiteral("Day-ahead forecast API did not return a job ID. status=%1").arg(status));
        return;
    }
    m_dayAheadJobStartedUtc = QDateTime::currentDateTimeUtc();
    qDebug().noquote() << "[cvxpy:day-ahead-job] submitted" << jobId
                       << "delivery_day=" << deliveryDay.toString(Qt::ISODate);
    pollDayAheadForecastJob(jobId, scenarioId, mode, deliveryDay, 0);
}

void StatnettClient::pollDayAheadForecastJob(
    const QString& jobId, const QString& scenarioId, const QString& mode, const QDate& deliveryDay, int pollCount)
{
    if (scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        return;
    }
    if (m_dayAheadJobStartedUtc.isValid()
        && m_dayAheadJobStartedUtc.secsTo(QDateTime::currentDateTimeUtc()) >= 600) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        emit fetchError(QStringLiteral("Day-ahead forecast job exceeded the 10-minute client polling window."));
        return;
    }
    QUrl url = m_apiBaseUrl;
    url.setPath(QStringLiteral("/api/optimization/day-ahead-forecast/jobs/%1").arg(jobId));
    QNetworkRequest request(url);
    request.setRawHeader("Accept", "application/json");
    request.setTransferTimeout(20000);
    QNetworkReply* reply = m_network.get(request);
    connect(reply, &QNetworkReply::finished, this, [this, reply, jobId, scenarioId, mode, deliveryDay, pollCount]() {
        onDayAheadJobReply(reply, jobId, scenarioId, mode, deliveryDay, pollCount);
    });
}

void StatnettClient::onDayAheadJobReply(
    QNetworkReply* reply, const QString& jobId, const QString& scenarioId,
    const QString& mode, const QDate& deliveryDay, int pollCount)
{
    if (reply->error() != QNetworkReply::NoError) {
        const bool superseded = scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode;
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        if (superseded) {
            reply->deleteLater();
            startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        } else {
            reportFailure(reply, QStringLiteral("greenpulse day-ahead forecast job"));
        }
        return;
    }
    const QByteArray data = reply->readAll();
    reply->deleteLater();
    const QJsonDocument document = QJsonDocument::fromJson(data);
    if (!document.isObject()) {
        const bool superseded = scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode;
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        if (superseded) {
            startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        } else {
            emit fetchError(QStringLiteral("Day-ahead job endpoint returned invalid JSON."));
        }
        return;
    }
    const QJsonObject root = document.object();
    const QString status = root.value(QStringLiteral("status")).toString();
    if (status == QStringLiteral("QUEUED") || status == QStringLiteral("RUNNING") || status == QStringLiteral("WAITING_FOR_FORECAST_ORIGIN")) {
        if ((scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode)) {
            m_dayAheadRequestActive = false;
            m_dayAheadJobStartedUtc = QDateTime();
            qDebug().noquote() << "[cvxpy:day-ahead-job] superseded scenario"
                               << "received=" << scenarioId << "active=" << m_currentDayAheadScenario << " mode=" << mode << " active_mode=" << m_currentDayAheadMode;
            startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
            return;
        }
        const int delayMs = qMin(2000, 750 + pollCount * 250);
        QTimer::singleShot(delayMs, this, [this, jobId, scenarioId, mode, deliveryDay, pollCount]() {
            pollDayAheadForecastJob(jobId, scenarioId, mode, deliveryDay, pollCount + 1);
        });
        return;
    }
    if ((scenarioId != m_currentDayAheadScenario || mode != m_currentDayAheadMode)) {
        m_dayAheadRequestActive = false;
        m_dayAheadJobStartedUtc = QDateTime();
        qDebug().noquote() << "[cvxpy:day-ahead-job] ignored stale scenario result"
                           << "job=" << jobId << "received=" << scenarioId
                           << "active=" << m_currentDayAheadScenario << " mode=" << mode << " active_mode=" << m_currentDayAheadMode;
        startDayAheadForecast(deliveryDay, m_currentDayAheadScenario, m_currentDayAheadMode);
        return;
    }
    m_dayAheadRequestActive = false;
    m_dayAheadJobStartedUtc = QDateTime();
    const QJsonObject result = root.value(QStringLiteral("result")).toObject();
    if (status == QStringLiteral("INPUTS_INCOMPLETE")) {
        const QJsonObject errors = result.value(QStringLiteral("errors_by_mtu")).toObject();
        m_latestTelemetry.sourceStatus = QStringLiteral("Day-ahead forecast inputs incomplete for %1 MTUs (%2 errors)")
            .arg(deliveryDay.toString(Qt::ISODate)).arg(errors.size());
        emit telemetryReceived(m_latestTelemetry);
        emit fetchError(m_latestTelemetry.sourceStatus);
        qWarning().noquote() << "[cvxpy:day-ahead-readiness] status=INPUTS_INCOMPLETE"
                             << "delivery_day=" << deliveryDay.toString(Qt::ISODate)
                             << "errors_by_mtu=" << errors;
        return;
    }
    if (status == QStringLiteral("FAILED") || result.isEmpty()) {
        const QString detail = root.value(QStringLiteral("error")).toString();
        emit fetchError(QStringLiteral("Day-ahead forecast job failed: %1").arg(detail));
        return;
    }
    if (status != QStringLiteral("SOLVED")
        && status != QStringLiteral("SOLVED_PARTIAL")
        && status != QStringLiteral("SOLVED_WITH_INFEASIBILITY")
        && status != QStringLiteral("SOLVED_WITH_UNKNOWN_FEASIBILITY")) {
        emit fetchError(QStringLiteral("Day-ahead forecast ended with unexpected status: %1").arg(status));
        return;
    }
    acceptDayAheadForecastResult(result, scenarioId);
    qDebug().noquote() << "[cvxpy:day-ahead-job] complete" << jobId
                       << "delivery_day=" << deliveryDay.toString(Qt::ISODate)
                       << "status=" << status;
}
