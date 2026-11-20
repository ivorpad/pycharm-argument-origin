package origin

import com.intellij.execution.configurations.RunProfile
import com.intellij.execution.executors.DefaultDebugExecutor
import com.intellij.execution.runners.ExecutionEnvironment
import com.intellij.execution.ui.RunContentDescriptor
import com.intellij.ide.plugins.PluginManagerCore
import com.intellij.openapi.extensions.PluginId
import com.intellij.util.execution.ParametersListUtil
import com.jetbrains.python.sdk.PythonSdkUtil
import com.jetbrains.python.debugger.PyDebugBackendRunner
import com.jetbrains.python.debugger.PyDebugRunner
import com.jetbrains.python.debugger.PyDebuggerBackend
import com.jetbrains.python.run.PythonRunConfiguration
import org.jetbrains.concurrency.Promise
import org.jetbrains.concurrency.resolvedPromise

// PyCharm chooses the backend before asking it for the launch state. Keep the
// original execution environment, including its session name and rerun configuration.
class RecordingBackend : PyDebugRunner(), PyDebugBackendRunner {
    override val backend = PyDebuggerBackend.PYDEVD

    override fun isApplicable(executorId: String, profile: RunProfile): Boolean =
        executorId == DefaultDebugExecutor.EXECUTOR_ID && profile is PythonRunConfiguration &&
            profile.envs["ARG_ORIGIN_RECORD"] != "0" && localScript(profile) && profile.scriptName != recordingLauncher()

    override fun startSession(environment: ExecutionEnvironment): Promise<RunContentDescriptor?> {
        val copy = recordingCopy(environment.runProfile as PythonRunConfiguration)
        val state = copy.getState(environment.executor, environment) ?: return resolvedPromise(null)
        return execute(environment, state)
    }
}

private fun localScript(config: PythonRunConfiguration): Boolean {
    if (config.isModuleMode || config.scriptName.isNullOrBlank()) return false
    val sdk = config.sdk ?: return false
    return !PythonSdkUtil.isRemote(sdk)
}

private fun recordingLauncher() = PluginManagerCore.getPlugin(PluginId.getId("local.argument.origin"))!!
    .pluginPath.resolve("python/record.py").toString()

private fun recordingCopy(original: PythonRunConfiguration): PythonRunConfiguration {
    val config = original.clone() as PythonRunConfiguration
    config.scriptParameters = listOf(ParametersListUtil.join(original.scriptName), original.scriptParameters.orEmpty())
        .filter { it.isNotBlank() }.joinToString(" ")
    config.scriptName = recordingLauncher()
    return config
}
