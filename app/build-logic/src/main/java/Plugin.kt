
import org.gradle.api.Plugin
import org.gradle.api.Project
import org.gradle.kotlin.dsl.provideDelegate
import java.io.File
import java.util.Properties
import java.util.Random

// Set non-zero value here to fix the random seed for reproducible builds
// CI builds are always reproducible
val RAND_SEED = if (System.getenv("CI") != null) 42 else 0
lateinit var RANDOM: Random
val ABI_SUPPORT_LIST = listOf("armeabi-v7a", "arm64-v8a", "x86", "x86_64", "riscv64")

const val APP_ID = "io.github.huskydg.magisk.next"
const val PRODUCT_CHANNEL = "next-system-experimental"
const val PRODUCT_NAME = "KitsuneMagisk Next"
const val UPSTREAM_BASE = "96221b69fae9910b1c0c75c2f92a4ebb2c2dc698"
const val VERSION_PREFIX = "31.0-kitsune-next"

private val props = Properties()
private var commitHash = ""
private var sourceRevision = ""
private var sourceTreeDirty = true
private val supportAbis = setOf("armeabi-v7a", "x86", "arm64-v8a", "x86_64", "riscv64")
private val defaultAbis = setOf("armeabi-v7a", "x86", "arm64-v8a", "x86_64")

private fun gitTreeDirty(root: File): Boolean {
    val process = ProcessBuilder(
        "git", "status", "--porcelain=v1", "--untracked-files=normal"
    ).directory(root).redirectErrorStream(true).start()
    val output = process.inputStream.bufferedReader().use { it.readText() }
    check(process.waitFor() == 0) {
        "Cannot inspect the Git worktree used for build identity: $output"
    }
    return output.isNotBlank()
}

object Config {
    operator fun get(key: String): String? {
        val v = props[key] as? String ?: return null
        return v.ifBlank { null }
    }

    fun contains(key: String) = get(key) != null

    val version: String get() {
        val version = get("version") ?: "$VERSION_PREFIX.$commitHash"
        return if (sourceTreeDirty) "$version-dirty" else version
    }
    val versionCode: Int get() = get("magisk.versionCode")!!.toInt()
    val stubVersion: String get() = get("magisk.stubVersion")!!
    val sourceCommit: String get() = sourceRevision
    val sourceDirty: Boolean get() = sourceTreeDirty
    val abiList: Set<String> get() {
        val abiList = get("abiList") ?: return defaultAbis
        return abiList.split(Regex("\\s*,\\s*")).toSet() intersect supportAbis
    }
}

fun Project.rootFile(path: String): File {
    val file = File(path)
    return if (file.isAbsolute) file
    else File(rootProject.file(".."), path)
}

class MagiskPlugin : Plugin<Project> {
    override fun apply(project: Project) = project.applyPlugin()

    private fun Project.applyPlugin() {
        initRandom(rootProject.file("dict.txt"))
        props.clear()

        // Get gradle properties relevant to Magisk
        props.putAll(providers.gradlePropertiesPrefixedBy("magisk.").get())

        // Load config.prop
        val configPath = findProperty("configPath") as String?
        val configFile = rootFile(configPath ?: "config.prop")
        if (configFile.exists()) {
            configFile.inputStream().use {
                val config = Properties()
                config.load(it)
                props.putAll(config)
            }
        }

        // Commandline override
        findProperty("abiList")?.let { props.put("abiList", it) }

        val git = ProcessBuilder("git", "rev-parse", "HEAD")
            .directory(rootFile(".")).redirectErrorStream(true).start()
        sourceRevision = git.inputStream.bufferedReader().use { it.readText().trim() }
        check(git.waitFor() == 0 && sourceRevision.matches(Regex("^[a-f0-9]{40}$"))) {
            "Cannot derive the full Git source identity"
        }
        commitHash = sourceRevision.take(8)
        sourceTreeDirty = gitTreeDirty(rootFile("."))
        findProperty("expectedSourceCommit")?.toString()?.let {
            check(it == sourceRevision) { "Git HEAD changed before Gradle accepted the build identity" }
        }
    }
}
