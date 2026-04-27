"""Custom dataset class that loads directly from HuggingFace Hub."""

import datasets as hf_datasets
from verl.utils.dataset.rl_dataset import RLHFDataset


class HFHubDataset(RLHFDataset):
    """RLHFDataset variant that accepts HuggingFace Hub paths.

    data_files should be of the form "owner/repo:split", e.g.
    "JetBrains-Research/django_method_gen:train".
    """

    def _download(self, use_origin_parquet=False):
        # Nothing to download for HF Hub paths
        pass

    def _read_files_and_tokenize(self):
        dataframes = []
        for path in self.data_files:
            if ":" in path.split("/")[-1]:
                repo, split = path.rsplit(":", 1)
            else:
                repo, split = path, "train"
            dataframes.append(hf_datasets.load_dataset(repo, split=split))
        self.dataframe = hf_datasets.concatenate_datasets(dataframes)

        import numpy as np

        total = len(self.dataframe)
        print(f"dataset len: {total}")

        if self.max_samples > 0 and self.max_samples < total:
            if self.shuffle:
                rngs_args = (self.seed,) if self.seed is not None else ()
                rng = np.random.default_rng(*rngs_args)
                indices = rng.choice(total, size=self.max_samples, replace=False)
            else:
                indices = np.arange(self.max_samples)
            self.dataframe = self.dataframe.select(indices.tolist())
            print(f"selected {self.max_samples} samples out of {total}")

        self.dataframe = self.maybe_filter_out_long_prompts(self.dataframe)
