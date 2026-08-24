
import org.eclipse.jgit.storage.file.FileRepositoryBuilder
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

const val APP_ID = "io.github.huskydg.magisk.next"
const val PRODUCT_CHANNEL = "next-system-experimental"
const val PRODUCT_NAME = "KitsuneMagisk Next"
const val UPSTREAM_BASE = "e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac"
const val VERSION_PREFIX = "30.7-kitsune-next"

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
        props.putAll(properties.filter { (key, _) -> key.startsWith("magisk.") })

        // Load config.prop
        val configPath: String? by this
        val configFile = rootFile(configPath ?: "config.prop")
        if (configFile.exists()) {
            configFile.inputStream().use {
                val config = Properties()
                config.load(it)
                // Remove properties that should be passed by commandline
                config.remove("abiList")
                props.putAll(config)
            }
        }

        // Commandline override
        findProperty("abiList")?.let { props.put("abiList", it) }

        FileRepositoryBuilder()
            .findGitDir(rootFile("."))
            .build()
            .use { repo ->
                val refId = repo.resolve("HEAD")
                    ?: error("Cannot resolve the Git HEAD used for build identity")
                sourceRevision = refId.name()
                commitHash = repo.newObjectReader().use {
                    it.abbreviate(refId, 8).name()
                }
                sourceTreeDirty = gitTreeDirty(rootFile("."))
                findProperty("expectedSourceCommit")?.toString()?.let {
                    check(it.matches(Regex("^[a-f0-9]{40}$")) && it == sourceRevision) {
                        "Git HEAD changed before Gradle accepted the build identity"
                    }
                }
            }
        check(sourceRevision.matches(Regex("^[a-f0-9]{40}$"))) {
            "Cannot derive the full Git source identity"
        }
    }
}
