import os
import random
import numpy as np
import librosa
import torch
import random
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn
from torch.utils.data import Dataset

progress = Progress(
    TextColumn("Loading: "),
    BarColumn(bar_width=80), "[progress.percentage]{task.percentage:>3.1f}%",
    "•",
    MofNCompleteColumn(),
    "•",
    TimeElapsedColumn(),
    "|",
    TimeRemainingColumn(),
    transient=True
    )

def traverse_dir(
        root_dir,
        extensions,
        amount=None,
        str_include=None,
        str_exclude=None,
        is_pure=False,
        is_sort=False,
        is_ext=True):
    file_list = []
    cnt = 0
    for root, _, files in os.walk(root_dir):
        for file in files:
            if any([file.endswith(f".{ext}") for ext in extensions]):
                # path
                mix_path = os.path.join(root, file)
                pure_path = mix_path[len(root_dir) + 1:] if is_pure else mix_path

                # amount
                if (amount is not None) and (cnt == amount):
                    if is_sort:
                        file_list.sort()
                    return file_list

                # check string
                if (str_include is not None) and (str_include not in pure_path):
                    continue
                if (str_exclude is not None) and (str_exclude in pure_path):
                    continue

                if not is_ext:
                    ext = pure_path.split('.')[-1]
                    pure_path = pure_path[:-(len(ext) + 1)]
                file_list.append(pure_path)
                cnt += 1
    if is_sort:
        file_list.sort()
    return file_list


def get_data_loaders(args, whole_audio=False, accelerator=None):
    if args.data.volume_noise == 0:
        volume_noise = None
    else:
        volume_noise = args.data.volume_noise
    data_train = AudioDataset(
        args.data.train_path,
        waveform_sec=args.data.duration,
        hop_size=args.data.block_size,
        sample_rate=args.data.sampling_rate,
        load_all_data=args.train.cache_all_data,
        whole_audio=whole_audio,
        extensions=args.data.extensions,
        n_spk=args.model.n_spk,
        device=args.train.cache_device,
        fp16=args.train.cache_fp16,
        use_aug=True,
        use_spk_encoder=args.model.use_speaker_encoder,
        spk_encoder_mode=args.data.speaker_encoder_mode,
        volume_noise=volume_noise,
        is_tts = args.model.is_tts,
        accelerator=accelerator
    )
    loader_train = torch.utils.data.DataLoader(
        data_train,
        batch_size=args.train.batch_size if not whole_audio else 1,
        shuffle=True,
        num_workers=args.train.num_workers if args.train.cache_device == 'cpu' else 0,
        persistent_workers=(args.train.num_workers > 0) if args.train.cache_device == 'cpu' else False,
        pin_memory=True if args.train.cache_device == 'cpu' else False
    )
    data_valid = AudioDataset(
        args.data.valid_path,
        waveform_sec=args.data.duration,
        hop_size=args.data.block_size,
        sample_rate=args.data.sampling_rate,
        load_all_data=args.train.cache_all_data,
        whole_audio=True,
        extensions=args.data.extensions,
        n_spk=args.model.n_spk,
        use_spk_encoder=args.model.use_speaker_encoder,
        spk_encoder_mode=args.data.speaker_encoder_mode,
        volume_noise=volume_noise,
        is_tts = args.model.is_tts,
        accelerator=None
    )
    loader_valid = torch.utils.data.DataLoader(
        data_valid,
        batch_size=1,
        shuffle=False,
        num_workers=0,
        pin_memory=True
    )
    return loader_train, loader_valid


class AudioDataset(Dataset):
    def __init__(
            self,
            path_root,
            waveform_sec,
            hop_size,
            sample_rate,
            load_all_data=True,
            whole_audio=False,
            extensions=['wav'],
            n_spk=1,
            device='cpu',
            fp16=False,
            use_aug=False,
            use_spk_encoder=False,
            spk_encoder_mode='each_spk',
            volume_noise=None,
            is_tts=True,
            accelerator=None
    ):
        super().__init__()

        self.waveform_sec = waveform_sec
        self.sample_rate = sample_rate
        self.hop_size = hop_size
        self.path_root = path_root
        self.use_spk_encoder = use_spk_encoder
        self.spk_encoder_mode = spk_encoder_mode
        self.paths = traverse_dir(
            os.path.join(path_root, 'audio'),
            extensions=extensions,
            is_pure=True,
            is_sort=True,
            is_ext=True
        )
        self.whole_audio = whole_audio
        self.use_aug = use_aug
        self.data_buffer = {}
        self.pitch_aug_dict = np.load(os.path.join(self.path_root, 'pitch_aug_dict.npy'), allow_pickle=True).item()
        self.is_tts = is_tts
        self.n_spk = n_spk
        self.spk_name_id_map = {}
        
        if accelerator is not None:
            self.paths = self.paths[accelerator.process_index::accelerator.num_processes]

        t_spk_id = 1
        with progress:
            load_task = progress.add_task("Test", total=len(self.paths))
            for name_ext in self.paths:
                path_audio = os.path.join(self.path_root, 'audio', name_ext)
                duration = librosa.get_duration(filename=path_audio, sr=self.sample_rate)

                if not is_tts:
                    path_f0 = os.path.join(self.path_root, 'f0', name_ext) + '.npy'
                    f0 = np.load(path_f0)
                    f0 = torch.from_numpy(f0).float().unsqueeze(-1).to(device)

                    path_volume = os.path.join(self.path_root, 'volume', name_ext) + '.npy'
                    volume = np.load(path_volume)
                    volume = torch.from_numpy(volume).float().unsqueeze(-1).to(device)
                    if volume_noise is not None:
                        _noise = volume_noise * torch.rand(volume.shape,).to(device)
                        volume = volume + _noise * torch.sign(volume)

                    path_augvol = os.path.join(self.path_root, 'aug_vol', name_ext) + '.npy'
                    aug_vol = np.load(path_augvol)
                    aug_vol = torch.from_numpy(aug_vol).float().unsqueeze(-1).to(device)
                    if volume_noise is not None:
                        _noise = volume_noise * torch.rand(aug_vol.shape,).to(device)
                        aug_vol = aug_vol + _noise * torch.sign(aug_vol)
                else:
                    f0 = None
                    aug_vol = None
                    volume = None
    
                if n_spk is not None and n_spk > 1:
                    dirname_split = os.path.dirname(name_ext)
                    if self.spk_name_id_map.get(dirname_split) is None:
                        self.spk_name_id_map[dirname_split] = t_spk_id
                        t_spk_id += 1
                    #print('==>', n_spk, t_spk_id, dirname_split, self.spk_name_id_map)
                    if t_spk_id < 1 or t_spk_id > n_spk + 1:
                        raise ValueError('[x] Muiti-speaker traing error : spk_id must be a positive integer from 1 to n_spk')
                else:
                    t_spk_id = 1
                spk_id = torch.LongTensor(np.array([t_spk_id])).to(device)

                if load_all_data:
                    path_mel = os.path.join(self.path_root, 'mel', name_ext) + '.npy'
                    mel = np.load(path_mel)
                    mel = torch.from_numpy(mel).to(device)
                    if not is_tts:
                        path_augmel = os.path.join(self.path_root, 'aug_mel', name_ext) + '.npy'
                        aug_mel = np.load(path_augmel)
                        aug_mel = torch.from_numpy(aug_mel).to(device)
                    else:
                        aug_mel = mel
                    path_units = os.path.join(self.path_root, 'units', name_ext) + '.npy'
                    units = np.load(path_units)
                    units_len = units.shape[0]
                    units = torch.from_numpy(units).to(device)

                    spk_emb = torch.rand(1, 1)
                    if use_spk_encoder and (spk_encoder_mode == 'each_spk'):
                        path_spk_emb_dict = os.path.join(self.path_root, 'spk_emb_dict.npy')
                        spk_emb = np.load(path_spk_emb_dict, allow_pickle=True).item()
                        spk_emb = spk_emb[str(t_spk_id)]
                        spk_emb = np.tile(spk_emb, (units_len, 1))
                        spk_emb = torch.from_numpy(spk_emb).to(device)

                    if use_spk_encoder and (spk_encoder_mode == 'each_wav'):
                        path_spk_emb = os.path.join(self.path_root, 'spk_emb', name_ext) + '.npy'
                        spk_emb = np.load(path_spk_emb)
                        spk_emb = torch.from_numpy(spk_emb).to(device)

                    self.data_buffer[name_ext] = {
                        'duration': duration,
                        'mel': mel,
                        'aug_mel': aug_mel,
                        'units': units,
                        'f0': f0,
                        'volume': volume,
                        'aug_vol': aug_vol,
                        'spk_id': spk_id,
                        't_spk_id': t_spk_id,
                        'spk_emb': spk_emb
                    }
                else:
                    self.data_buffer[name_ext] = {
                        'duration': duration,
                        'f0': f0,
                        'volume': volume,
                        'aug_vol': aug_vol,
                        'spk_id': spk_id,
                        't_spk_id': t_spk_id
                    }
                progress.update(load_task, advance=1)

    def __getitem__(self, file_idx):
        try:
            name_ext = self.paths[file_idx]
            data_buffer = self.data_buffer[name_ext]
            # check duration. if too short, then skip
            if data_buffer['duration'] < (self.waveform_sec + 0.1):
                return self.__getitem__((file_idx + 1) % len(self.paths))

            # get item
            return self.get_data(name_ext, data_buffer)
        except Exception as e:
            return self.__getitem__((file_idx + 1) % len(self.paths))

    def get_data(self, name_ext, data_buffer):
        name = os.path.splitext(name_ext)[0]
        frame_resolution = self.hop_size / self.sample_rate
        duration = data_buffer['duration']
        waveform_sec = duration if self.whole_audio else self.waveform_sec

        idx_from = 0 if self.whole_audio else random.uniform(0, duration - waveform_sec - 0.1)
        start_frame = int(idx_from / frame_resolution)
        units_frame_len = int(waveform_sec / frame_resolution)
        aug_flag = random.choice([True, False]) and self.use_aug and not self.is_tts

        mel_key = 'aug_mel' if aug_flag else 'mel'
        mel = data_buffer.get(mel_key)
        if mel is None:
            mel = os.path.join(self.path_root, mel_key, name_ext) + '.npy'
            mel = np.load(mel)
            mel = mel[start_frame: start_frame + units_frame_len]
            mel = torch.from_numpy(mel).float()
        else:
            mel = mel[start_frame: start_frame + units_frame_len]

        units = data_buffer.get('units')
        if units is None:
            units = os.path.join(self.path_root, 'units', name_ext) + '.npy'
            units = np.load(units)
            units_len = units.shape[0]
            units = units[start_frame: start_frame + units_frame_len]
            units = torch.from_numpy(units).float()
        else:
            units = units[start_frame: start_frame + units_frame_len]

        spk_emb = data_buffer.get('spk_emb')
        if self.use_spk_encoder:
            if spk_emb is None:
                spk_emb = os.path.join(self.path_root, 'spk_emb', name_ext) + '.npy'
                if self.spk_encoder_mode == 'each_wav':
                    spk_emb = np.load(spk_emb)
                elif self.spk_encoder_mode == 'each_spk':
                    path_spk_emb_dict = os.path.join(self.path_root, 'spk_emb_dict.npy')
                    t_spk_id = data_buffer.get('t_spk_id')
                    spk_emb = np.load(path_spk_emb_dict, allow_pickle=True).item()
                    spk_emb = spk_emb[str(t_spk_id)]
                    spk_emb = np.tile(spk_emb, (units_len, 1))
                spk_emb = spk_emb[start_frame: start_frame + units_frame_len]
                spk_emb = torch.from_numpy(spk_emb).float()
            else:
                spk_emb = spk_emb[start_frame: start_frame + units_frame_len]
        else:
            spk_emb = torch.rand(1, 1)
        if not self.is_tts:

            f0 = data_buffer.get('f0')
            aug_shift = 0
            if aug_flag:
                aug_shift = self.pitch_aug_dict[name_ext]
            f0_frames = 2 ** (aug_shift / 12) * f0[start_frame: start_frame + units_frame_len]

            vol_key = 'aug_vol' if aug_flag else 'volume'
            volume = data_buffer.get(vol_key)
            volume_frames = volume[start_frame: start_frame + units_frame_len]

            aug_shift = torch.from_numpy(np.array([[aug_shift]])).float()
        else:
            aug_shift = np.array([-1])
            f0_frames = np.array([-1])
            volume_frames = np.array([-1])

        spk_id = data_buffer.get('spk_id')

        return dict(mel=mel, f0=f0_frames, volume=volume_frames, units=units, spk_id=spk_id, aug_shift=aug_shift, name=name, name_ext=name_ext, spk_emb=spk_emb)

    def __len__(self):
        return len(self.paths)
