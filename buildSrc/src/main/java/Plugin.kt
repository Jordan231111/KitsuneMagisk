
import org.eclipse.jgit.lib.Constants
import org.eclipse.jgit.storage.file.FileRepositoryBuilder
import org.gradle.api.GradleException
import org.gradle.api.Plugin
import org.gradle.api.Project
import org.gradle.kotlin.dsl.provideDelegate
import java.io.File
import java.util.*

private val props = Properties()
private var commitHash = ""

object Config {
    operator fun get(key: String): String? {
        val v = props[key] as? String ?: return null
        return if (v.isBlank()) null else v
    }

    fun contains(key: String) = get(key) != null

    val version: String get() = get("version") ?: commitHash
    val versionCode: Int get() = get("magisk.versionCode")!!.toInt()
    val stubVersion: String get() = get("magisk.stubVersion")!!
}

class MagiskPlugin : Plugin<Project> {
    override fun apply(project: Project) = project.applyPlugin()

    private fun Project.applyPlugin() {
        initRandom(rootProject.file("dict.txt"))
        props.clear()
        rootProject.file("gradle.properties").inputStream().use { props.load(it) }
        val configPath: String? by this
        val config = configPath?.let { File(it) } ?: rootProject.file("config.prop")
        if (config.exists())
            config.inputStream().use { props.load(it) }

        commitHash = Config["version"] ?: run {
            val builder = FileRepositoryBuilder()
                .readEnvironment()
                .findGitDir(rootProject.rootDir)
            if (builder.gitDir == null) {
                throw GradleException(
                    "Cannot determine the source revision; set version in a custom config.prop"
                )
            }
            builder.build().use { repo ->
                val refId = repo.resolve(Constants.HEAD)
                    ?: throw GradleException("Cannot resolve the Git HEAD revision")
                repo.newObjectReader().use { reader ->
                    "${reader.abbreviate(refId, 8).name()}-kitsune"
                }
            }
        }
        if (!commitHash.contains("kitsune")) {
            throw GradleException(
                "Version must contain the lowercase Kitsune identity marker 'kitsune'"
            )
        }
    }
}
