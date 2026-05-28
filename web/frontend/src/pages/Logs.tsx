import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { GenerationLog } from "../api/types";
import PageMeta from "../components/common/PageMeta";

export default function Logs() {
  const [generations, setGenerations] = useState<GenerationLog[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<string>("");
  const [selectedFile, setSelectedFile] = useState<string>("");
  const [logContent, setLogContent] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.generations()
      .then((gens) => {
        setGenerations(gens);
        if (gens.length > 0) {
          setSelectedVersion(gens[gens.length - 1].version);
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (selectedVersion) {
      const gen = generations.find((g) => g.version === selectedVersion);
      if (gen && gen.files.length > 0 && !gen.files.includes(selectedFile)) {
        setSelectedFile(gen.files[0]);
      }
    }
  }, [selectedVersion, generations]);

  useEffect(() => {
    if (selectedVersion && selectedFile) {
      api.logContent(selectedVersion, selectedFile, 500)
        .then((res) => setLogContent(res.content))
        .catch(() => setLogContent("Error loading log"));
    }
  }, [selectedVersion, selectedFile]);

  const currentGen = generations.find((g) => g.version === selectedVersion);

  if (loading) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">Loading...</div>;
  }

  return (
    <>
      <PageMeta title="Generation Logs — Evolution Dashboard" description="LLM conversation logs per generation" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Generation Logs</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            LLM conversation logs (master, worker, reviewer) per generation
          </p>
        </div>

        <div className="flex">
          {/* Version list */}
          <div className="w-48 border-r border-gray-100 dark:border-gray-800 overflow-y-auto max-h-[600px]">
            {generations.map((gen) => (
              <button
                key={gen.version}
                onClick={() => setSelectedVersion(gen.version)}
                className={`w-full px-4 py-3 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800/50 ${
                  selectedVersion === gen.version
                    ? "bg-blue-50 text-blue-700 font-medium dark:bg-blue-900/20 dark:text-blue-400"
                    : "text-gray-700 dark:text-gray-300"
                }`}
              >
                {gen.version}
                <span className="block text-xs text-gray-400">{gen.files.length} files</span>
              </button>
            ))}
          </div>

          {/* File list + content */}
          <div className="flex-1">
            {currentGen && (
              <div className="border-b border-gray-100 dark:border-gray-800 flex gap-1 px-3 py-2 overflow-x-auto">
                {currentGen.files.map((file) => (
                  <button
                    key={file}
                    onClick={() => setSelectedFile(file)}
                    className={`px-3 py-1.5 rounded-lg text-xs whitespace-nowrap ${
                      selectedFile === file
                        ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                        : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800/50"
                    }`}
                  >
                    {file}
                  </button>
                ))}
              </div>
            )}

            <div className="p-4">
              <pre className="text-xs text-gray-700 dark:text-gray-300 overflow-auto max-h-[500px] whitespace-pre-wrap font-mono leading-relaxed bg-gray-50 dark:bg-gray-900 rounded-lg p-4">
                {logContent || "Select a file to view"}
              </pre>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
