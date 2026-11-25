#pragma once

#include <QDateTime>
#include <QJsonObject>
#include <QJsonValue>
#include <QString>
#include <QStringList>

namespace StatnettReplyUtils {
inline QString utcNow()
{
    return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
}

inline QString elementText(const QJsonObject& object, const QStringList& keys)
{
    for (const QString& key : keys) {
        const QJsonValue value = object.value(key);
        if (!value.isUndefined() && !value.isNull())
            return value.toVariant().toString();
    }
    return {};
}

inline bool parseNumber(const QString& text, double* output)
{
    if (!output)
        return false;
    bool ok = false;
    const double value = text.toDouble(&ok);
    if (ok)
        *output = value;
    return ok;
}

inline QDateTime jsonDateTime(const QJsonValue& value)
{
    if (value.isDouble())
        return QDateTime::fromMSecsSinceEpoch(static_cast<qint64>(value.toDouble()), Qt::UTC);
    const QString text = value.toString().trimmed();
    if (text.isEmpty())
        return {};
    bool epochOk = false;
    const qint64 epochValue = text.toLongLong(&epochOk);
    if (epochOk && epochValue > 1000000000LL) {
        const qint64 epochMs = epochValue > 100000000000LL ? epochValue : epochValue * 1000LL;
        return QDateTime::fromMSecsSinceEpoch(epochMs, Qt::UTC);
    }
    QDateTime parsed = QDateTime::fromString(text, Qt::ISODateWithMs);
    if (!parsed.isValid())
        parsed = QDateTime::fromString(text, Qt::ISODate);
    if (parsed.isValid() && parsed.timeSpec() == Qt::LocalTime)
        parsed.setTimeSpec(Qt::UTC);
    return parsed.isValid() ? parsed.toUTC() : QDateTime();
}
} // namespace StatnettReplyUtils
