package com.topjohnwu.magisk.core.data.magiskdb

import com.topjohnwu.magisk.core.AppContext
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.model.su.SuPolicy

private const val SELECT_QUERY = "SELECT (until - strftime(\"%s\", \"now\")) AS remain, *"

class PolicyDao : MagiskDB() {

    suspend fun deleteOutdated() {
        val query = "DELETE FROM ${Table.POLICY} WHERE " +
            "(until > 0 AND until < strftime(\"%s\", \"now\")) OR until < 0"
        exec(query)

    }

    suspend fun delete(uid: Int) {
        val query = "DELETE FROM ${Table.POLICY} WHERE uid=$uid"
        exec(query)
        val count = exec("SELECT count(*) AS remaining FROM ${Table.POLICY} WHERE uid=$uid") {
            it["remaining"]?.toIntOrNull()
        }.singleOrNull()
        if (count != 0) throw java.io.IOException("Could not confirm policy removal")
    }

    suspend fun fetch(uid: Int): SuPolicy? {
        val query = "$SELECT_QUERY FROM ${Table.POLICY} WHERE uid=$uid LIMIT 1"
        return exec(query, ::toPolicy).firstOrNull()
    }

    suspend fun update(policy: SuPolicy) {
        val map = policy.toMap()
        if (!Const.Version.atLeast_25_0()) {
            // Put in package_name for old database
            map["package_name"] = AppContext.packageManager.getNameForUid(policy.uid)!!
        }
        val query = "REPLACE INTO ${Table.POLICY} ${map.toQuery()}"
        exec(query)
        val saved = fetch(policy.uid)
        if (saved == null || saved.policy != policy.policy || saved.logging != policy.logging ||
            saved.notification != policy.notification ||
            (policy.remain <= 0 && saved.remain != policy.remain) ||
            (policy.remain > 0 && saved.remain !in (policy.remain - 5).coerceAtLeast(1)..policy.remain)) {
            throw java.io.IOException("Could not confirm policy update")
        }
    }

    suspend fun fetchAll(): List<SuPolicy> {
        val query = "$SELECT_QUERY FROM ${Table.POLICY} WHERE uid/100000=${Const.USER_ID} " +
            "AND (until=0 OR until>strftime(\"%s\", \"now\"))"
        return exec(query, ::toPolicy).filterNotNull()
    }

    private fun toPolicy(map: Map<String, String>): SuPolicy? {
        val uid = map["uid"]?.toInt() ?: return null
        val policy = SuPolicy(uid)

        map["until"]?.toLong()?.let { until ->
            if (until <= 0) {
                policy.remain = until
            } else {
                map["remain"]?.toLong()?.let { policy.remain = it }
            }
        }

        map["policy"]?.toInt()?.let { policy.policy = it }
        map["logging"]?.toInt()?.let { policy.logging = it != 0 }
        map["notification"]?.toInt()?.let { policy.notification = it != 0 }
        return policy
    }

}
