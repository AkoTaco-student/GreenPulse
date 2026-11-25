#include "StatnettClient.hpp"

#include <QDateTime>
#include <QTimeZone>
#include <QDebug>
#include <QTimer>
#include <QUrlQuery>
#include <QMap>
#include <QSet>
#include <QList>
#include <QPair>
#include <QStringList>
#include <QVariantMap>
#include <QJsonDocument>
#include <QJsonValue>
#include <QJsonArray>
#include <QNetworkRequest>
#include <QXmlStreamReader>
#include <QRegularExpression>
#include <cmath>
#include <limits>

StatnettClient::StatnettClient(QObject* parent)
    : QObject(parent),
      m_apiBaseUrl(configuredUrl("greenpulse_API_BASE_URL", QStringLiteral("http://127.0.0.1:8000")))
{
}

QUrl StatnettClient::configuredUrl(const char* environmentName, const QString& defaultUrl) const
{
    QString overrideValue = qEnvironmentVariable(environmentName).trimmed();
    // Accept the documented GRIDPULSE_* spelling as well as the historic
    // lower-case greenpulse_* names already used by this client.
    if (overrideValue.isEmpty()) {
        const QByteArray legacyName(environmentName);
        if (legacyName.startsWith("greenpulse_")) {
            const QByteArray documentedName = QByteArray("GRIDPULSE_") + legacyName.mid(10);
            overrideValue = qEnvironmentVariable(documentedName.constData()).trimmed();
        }
    }
    return QUrl::fromUserInput(overrideValue.isEmpty() ? defaultUrl : overrideValue);
}

void StatnettClient::fetchLatestPrices()
{
    const QUrl url = configuredUrl("greenpulse_MARKET_PRICES_URL",
        QStringLiteral("http://127.0.0.1:8000/api/market/prices"));
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("greenpulse/1.1"));
    request.setRawHeader("Accept", "application/json");
    QNetworkReply* reply = m_network.get(request);
    //qDebug().noquote() << "[feed:prices] GET" << url.toString(QUrl::RemoveQuery)
     //                  << "started" << utcNow();
    connect(reply, &QNetworkReply::finished, this, [this, reply]() { onPricesReply(reply); });
}

void StatnettClient::fetchPhysicalFlows()
{
    const QUrl url = configuredUrl("greenpulse_STATNETT_FLOW_URL",
        QStringLiteral("https://driftsdata.statnett.no/restapi/PhysicalFlowMap/GetFlow"));
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("greenpulse/1.1"));
    request.setRawHeader("Accept", "application/xml, text/xml, application/json");
    QNetworkReply* reply = m_network.get(request);
    //qDebug().noquote() << "[feed:physical-flow] GET" << url.toString(QUrl::RemoveQuery)
     //                  << "started" << utcNow();
    connect(reply, &QNetworkReply::finished, this, [this, reply]() { onFlowsReply(reply); });
}

void StatnettClient::fetchFrequency()
{
    const QUrl url = configuredUrl("greenpulse_STATNETT_FREQUENCY_URL",
        QStringLiteral("https://driftsdata.statnett.no/restapi/Frequency/ByMinute"));
    QUrl frequencyUrl = url;
    QUrlQuery query(frequencyUrl);
    const QDate osloDate = QDateTime::currentDateTime(QTimeZone(QByteArrayLiteral("Europe/Oslo"))).date();
    if (!query.hasQueryItem(QStringLiteral("From"))) {
        query.addQueryItem(QStringLiteral("From"), osloDate.toString(Qt::ISODate));
        frequencyUrl.setQuery(query);
    }
    QNetworkRequest request(frequencyUrl);
    request.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("greenpulse/1.1"));
    request.setRawHeader("Accept", "application/json, application/xml, text/xml, */*");
    request.setTransferTimeout(45000);
    QNetworkReply* reply = m_network.get(request);
    //qDebug().noquote() << "[feed:frequency] GET" << frequencyUrl.toString(QUrl::RemoveQuery)
     //                  << "started" << utcNow();
    connect(reply, &QNetworkReply::finished, this, [this, reply]() { onFrequencyReply(reply); });
}

void StatnettClient::reportFailure(QNetworkReply* reply, const QString& endpoint)
{
    const int httpStatus = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QByteArray responseBody = reply->readAll();
    QString detail = reply->errorString();

    const QJsonDocument response = QJsonDocument::fromJson(responseBody);
    if (response.isObject()) {
        const QJsonValue apiDetail = response.object().value(QStringLiteral("detail"));
        if (apiDetail.isString())
            detail = apiDetail.toString();
        else if (apiDetail.isObject())
            detail = QString::fromUtf8(QJsonDocument(apiDetail.toObject()).toJson(QJsonDocument::Compact));
        else if (apiDetail.isArray())
            detail = QString::fromUtf8(QJsonDocument(apiDetail.toArray()).toJson(QJsonDocument::Compact));
    }
    if (detail.isEmpty() && !responseBody.isEmpty())
        detail = QString::fromUtf8(responseBody.left(1000));

    qWarning().noquote() << "[feed:error]" << endpoint
                         << "http=" << httpStatus
                         << "network=" << reply->error()
                         << "detail=" << detail
                         << "response_body=" << QString::fromUtf8(responseBody.left(1000));
    emit fetchError(QStringLiteral("%1: HTTP %2 — %3").arg(endpoint).arg(httpStatus).arg(detail));
    reply->deleteLater();
}
