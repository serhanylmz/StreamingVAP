import torch
from torch import Tensor
from typing import Dict, List, Tuple

from vap.objective import VAPObjective, Codebook
from vap.utils.utils import get_dialog_states, find_island_idx_len


def last_speaker_single(s):
    assert s.ndim == 1, f"Expected 1D tensor, got {s.ndim}D tensor"
    start, _, val = find_island_idx_len(s)

    # exlude silences (does not effect last_speaker)
    # silences should be the value of the previous speaker
    sil_idx = torch.where(val == 1)[0]
    if len(sil_idx) > 0:
        if sil_idx[0] == 0:
            val[0] = 2  # 2 is both we don't know if its a shift or hold
            sil_idx = sil_idx[1:]
        val[sil_idx] = val[sil_idx - 1]
    # map speaker B state (=3) to 1
    val[val == 3] = 1
    # get repetition lengths
    repeat = start[1:] - start[:-1]
    # Find difference between original and repeated
    # and use diff to repeat the last speaker until the end of segment
    diff = len(s) - repeat.sum(0)
    repeat = torch.cat((repeat, diff.unsqueeze(0)))
    # repeat values to create last speaker over entire segment
    last_speaker = torch.repeat_interleave(val, repeat)
    return last_speaker


def get_last_speaker(vad: Tensor) -> Tensor:
    ds = get_dialog_states(vad)
    if ds.ndim > 1:
        ls = []
        for s in ds:
            ls.append(last_speaker_single(s))
        ls = torch.stack(ls)
    else:
        ls = last_speaker_single(ds)
    return ls


def end_of_segment_mono(n: int, max: int = 3) -> Tensor:
    """
    # 0, 0, 0, 0
    # 1, 0, 0, 0
    # 1, 1, 0, 0
    # 1, 1, 1, 0
    """
    v = torch.zeros((max + 1, n))
    for i in range(max):
        v[i + 1, : i + 1] = 1
    return v


def all_permutations_mono(n: int, start: int = 0) -> Tensor:
    vectors = []
    for i in range(start, 2**n):
        i = bin(i).replace("0b", "").zfill(n)
        tmp = torch.zeros(n)
        for j, val in enumerate(i):
            tmp[j] = float(val)
        vectors.append(tmp)
    return torch.stack(vectors)


def on_activity_change_mono(n: int = 4, min_active: int = 2) -> Tensor:
    """

    Used where a single speaker is active. This vector (single speaker) represents
    the classes we use to infer that the current speaker will end their activity
    and the other take over.

    the `min_active` variable corresponds to the minimum amount of frames that must
    be active AT THE END of the projection window (for the next active speaker).
    This used to not include classes where the activity may correspond to a short backchannel.
    e.g. if only the last bin is active it may be part of just a short backchannel, if we require 2 bins to
    be active we know that the model predicts that the continuation will be at least 2 bins long and thus
    removes the ambiguouty (to some extent) about the prediction.
    """

    base = torch.zeros(n)
    # force activity at the end
    if min_active > 0:
        base[-min_active:] = 1

    # get all permutations for the remaining bins
    permutable = n - min_active
    if permutable > 0:
        perms = all_permutations_mono(permutable)
        base = base.repeat(perms.shape[0], 1)
        base[:, :permutable] = perms
    return base


def combine_speakers(x1: Tensor, x2: Tensor, mirror: bool = False) -> Tensor:
    if x1.ndim == 1:
        x1 = x1.unsqueeze(0)
    if x2.ndim == 1:
        x2 = x2.unsqueeze(0)
    vad = []
    for a in x1:
        for b in x2:
            vad.append(torch.stack((a, b), dim=0))

    vad = torch.stack(vad)
    if mirror:
        vad = torch.stack((vad, torch.stack((vad[:, 1], vad[:, 0]), dim=1)))
    return vad


def sort_idx(x: Tensor) -> Tensor:
    if x.ndim == 1:
        x, _ = x.sort()
    elif x.ndim == 2:
        if x.shape[0] == 2:
            a, _ = x[0].sort()
            b, _ = x[1].sort()
            x = torch.stack((a, b))
        else:
            x, _ = x[0].sort()
            x = x.unsqueeze(0)
    return x


# TODO: make the zero-shot from paper accessible
class ZeroShotOld(VAPObjective):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subset_silence, self.subset_silence_hold = self.init_subset_silence()
        self.subset_active, self.subset_active_hold = self.init_subset_active()
        self.bc_prediction = self.init_subset_backchannel()

    def init_subset_silence(self) -> Tuple[Tensor, Tensor]:
        """
        During mutual silences we wish to infer which speaker the model deems most likely.

        We focus on classes where only a single speaker is active in the projection window,
        renormalize the probabilities on this subset, and determine which speaker is the most
        likely next speaker.
        """

        # active channel: At least 1 bin is active -> all permutations (all except the no-activity)
        # active = self._all_permutations_mono(n, start=1)  # at least 1 active
        # active channel: At least 1 bin is active -> all permutations (all except the no-activity)
        active = on_activity_change_mono(self.codebook.n_bins, min_active=2)
        # non-active channel: zeros
        non_active = torch.zeros((1, active.shape[-1]))
        # combine
        shift_oh = combine_speakers(active, non_active, mirror=True)
        shift = self.codebook.encode(shift_oh)
        shift = sort_idx(shift)

        # symmetric, this is strictly unneccessary but done for convenience and to be similar
        # to 'get_on_activity_shift' where we actually have asymmetric classes for hold/shift
        hold = shift.flip(0)
        return shift, hold

    def init_subset_active(self) -> Tuple[Tensor, Tensor]:
        """On activity"""
        # Shift subset
        eos = end_of_segment_mono(self.codebook.n_bins, max=2)
        nav = on_activity_change_mono(self.codebook.n_bins, min_active=2)
        shift_oh = combine_speakers(nav, eos, mirror=True)
        shift = self.codebook.encode(shift_oh)
        shift = sort_idx(shift)

        # Don't shift subset
        eos = on_activity_change_mono(self.codebook.n_bins, min_active=2)
        zero = torch.zeros((1, self.codebook.n_bins))
        hold_oh = combine_speakers(zero, eos, mirror=True)
        hold = self.codebook.encode(hold_oh)
        hold = sort_idx(hold)
        return shift, hold

    def init_subset_backchannel(self, n: int = 4) -> Tensor:
        if n != 4:
            raise NotImplementedError("Not implemented for bin-size != 4")

        # at least 1 bin active over 3 bins
        bc_speaker = all_permutations_mono(n=3, start=1)
        bc_speaker = torch.cat(
            (bc_speaker, torch.zeros((bc_speaker.shape[0], 1))), dim=-1
        )

        # all permutations of 3 bins
        current = all_permutations_mono(n=3, start=0)
        current = torch.cat((current, torch.ones((current.shape[0], 1))), dim=-1)

        bc_both = combine_speakers(bc_speaker, current, mirror=True)
        return self.codebook.encode(bc_both)

    def marginal_probs(self, probs: Tensor, pos_idx: Tensor, neg_idx: Tensor) -> Tensor:
        p = []
        for next_speaker in [0, 1]:
            joint = torch.cat((pos_idx[next_speaker], neg_idx[next_speaker]), dim=-1)
            p_sum = probs[..., joint].sum(dim=-1)
            p.append(probs[..., pos_idx[next_speaker]].sum(dim=-1) / p_sum)
        return torch.stack(p, dim=-1)

    def probs_on_silence(self, probs: Tensor) -> Tensor:
        return self.marginal_probs(probs, self.subset_silence, self.subset_silence_hold)

    def probs_on_active(self, probs: Tensor) -> Tensor:
        return self.marginal_probs(probs, self.subset_active, self.subset_active_hold)

    def probs_backchannel(self, probs: Tensor) -> Tensor:
        ap = probs[..., self.bc_prediction[0]].sum(-1)
        bp = probs[..., self.bc_prediction[1]].sum(-1)
        return torch.stack((ap, bp), dim=-1)

    def silence_probs(
        self,
        p_a: Tensor,
        p_b: Tensor,
        sil_probs: Tensor,
        silence: Tensor,
    ) -> Tuple[Tensor, Tensor]:
        w = torch.where(silence)
        p_a[w] = sil_probs[w][..., 0]
        p_b[w] = sil_probs[w][..., 1]
        return p_a, p_b

    def single_speaker_probs(
        self,
        p0: Tensor,
        p1: Tensor,
        act_probs: Tensor,
        current: Tensor,
        other_speaker: int,
    ) -> Tuple[Tensor, Tensor]:
        w = torch.where(current)
        p0[w] = 1 - act_probs[w][..., other_speaker]  # P_a = 1-P_b
        p1[w] = act_probs[w][..., other_speaker]  # P_b
        return p0, p1

    def overlap_probs(
        self, p_a: Tensor, p_b: Tensor, act_probs: Tensor, both: Tensor
    ) -> Tuple[Tensor, Tensor]:
        """
        P_a_prior=A is next (active)
        P_b_prior=B is next (active)
        We the compare/renormalize given the two values of A/B is the next speaker
        sum = P_a_prior+P_b_prior
        P_a = P_a_prior / sum
        P_b = P_b_prior / sum
        """
        w = torch.where(both)
        # Re-Normalize and compare next-active
        sum = act_probs[w][..., 0] + act_probs[w][..., 1]

        p_a[w] = act_probs[w][..., 0] / sum
        p_b[w] = act_probs[w][..., 1] / sum
        return p_a, p_b

    def probs_next_speaker(self, probs: Tensor, va: Tensor) -> Tensor:
        """
        Extracts the probabilities for who the next speaker is dependent on what the current moment is.

        This means that on mutual silences we use the 'silence'-subset,
        where a single speaker is active we use the 'active'-subset and where on overlaps
        """
        sil_probs = self.probs_on_silence(probs)
        act_probs = self.probs_on_active(probs)

        # Start wit all zeros
        # p_a: probability of A being next speaker (channel: 0)
        # p_b: probability of B being next speaker (channel: 1)
        p_a = torch.zeros_like(va[..., 0])
        p_b = torch.zeros_like(va[..., 0])

        # dialog states
        ds = get_dialog_states(va)
        silence = ds == 1
        a_current = ds == 0
        b_current = ds == 3
        both = ds == 2

        # silence
        p_a, p_b = self.silence_probs(p_a, p_b, sil_probs, silence)

        # A current speaker
        # Given only A is speaking we use the 'active' probability of B being the next speaker
        p_a, p_b = self.single_speaker_probs(
            p_a, p_b, act_probs, a_current, other_speaker=1
        )

        # B current speaker
        # Given only B is speaking we use the 'active' probability of A being the next speaker
        p_b, p_a = self.single_speaker_probs(
            p_b, p_a, act_probs, b_current, other_speaker=0
        )

        # Both
        p_a, p_b = self.overlap_probs(p_a, p_b, act_probs, both)

        p_next_speaker = torch.stack((p_a, p_b), dim=-1)
        return p_next_speaker

    def get_probs(self, logits: Tensor, va: Tensor) -> Dict[str, Tensor]:
        probs = logits.softmax(-1)
        nmax = probs.shape[-2]
        p = self.probs_next_speaker(probs, va[:, :nmax])
        p_bc = self.probs_backchannel(probs)
        return {"p": p, "p_bc": p_bc}

    @torch.no_grad()
    def extract_prediction_and_targets(
        self,
        p: Tensor,
        p_bc: Tensor,
        events: Dict[str, List[List[Tuple[int, int, int]]]],
        device=None,
    ) -> Tuple[Dict[str, Tensor], Dict[str, Tensor]]:
        batch_size = len(events["hold"])

        preds = {"hs": [], "pred_shift": [], "ls": [], "pred_backchannel": []}
        targets = {"hs": [], "pred_shift": [], "ls": [], "pred_backchannel": []}
        for b in range(batch_size):
            ###########################################
            # Hold vs Shift
            ###########################################
            # The metrics (i.e. shift/hold) are binary so we must decide
            # which 'class' corresponds to which numeric label
            # we use Holds=0, Shifts=1
            for start, end, speaker in events["shift"][b]:
                pshift = p[b, start:end, speaker]
                preds["hs"].append(pshift)
                targets["hs"].append(torch.ones_like(pshift))
            for start, end, speaker in events["hold"][b]:
                phold = 1 - p[b, start:end, speaker]
                preds["hs"].append(phold)
                targets["hs"].append(torch.zeros_like(phold))
            ###########################################
            # Shift-prediction
            ###########################################
            for start, end, speaker in events["pred_shift"][b]:
                # prob of next speaker -> the correct next speaker i.e. a SHIFT
                pshift = p[b, start:end, speaker]
                preds["pred_shift"].append(pshift)
                targets["pred_shift"].append(torch.ones_like(pshift))
            for start, end, speaker in events["pred_shift_neg"][b]:
                # prob of next speaker -> the correct next speaker i.e. a HOLD
                phold = 1 - p[b, start:end, speaker]  # 1-shift = Hold
                preds["pred_shift"].append(phold)
                # Negatives are zero -> hold predictions
                targets["pred_shift"].append(torch.zeros_like(phold))
            ###########################################
            # Backchannel-prediction
            ###########################################
            for start, end, speaker in events["pred_backchannel"][b]:
                # prob of next speaker -> the correct next backchanneler i.e. a Backchannel
                pred_bc = p_bc[b, start:end, speaker]
                preds["pred_backchannel"].append(pred_bc)
                targets["pred_backchannel"].append(torch.ones_like(pred_bc))
            for start, end, speaker in events["pred_backchannel_neg"][b]:
                # prob of 'speaker' making a 'backchannel' in the close future
                # over these negatives this probability should be low -> 0
                # so no change of probability have to be made (only the labels are now zero)
                pred_bc = p_bc[b, start:end, speaker]  # 1-shift = Hold
                preds["pred_backchannel"].append(
                    pred_bc
                )  # Negatives are zero -> hold predictions
                targets["pred_backchannel"].append(torch.zeros_like(pred_bc))
            ###########################################
            # Long vs Shoft
            ###########################################
            # TODO: Should this be the same as backchannel
            # or simply next speaker probs?
            for start, end, speaker in events["long"][b]:
                # prob of next speaker -> the correct next speaker i.e. a LONG
                plong = p[b, start:end, speaker]
                preds["ls"].append(plong)
                targets["ls"].append(torch.ones_like(plong))
            for start, end, speaker in events["short"][b]:
                # the speaker in the 'short' events is the speaker who
                # utters a short utterance: p[b, start:end, speaker] means:
                # the  speaker saying something short has this probability
                # of continue as a 'long'
                # Therefore to correctly predict a 'short' entry this probability
                # should be low -> 0
                # thus we do not have to subtract the prob from 1 (only the labels are now zero)
                # prob of next speaker -> the correct next speaker i.e. a SHORT
                pshort = p[b, start:end, speaker]  # 1-shift = Hold
                preds["ls"].append(pshort)
                # Negatives are zero -> short predictions
                targets["ls"].append(torch.zeros_like(pshort))

        # cat/stack/flatten to single tensor
        device = device if device is not None else p.device
        out_preds = {}
        out_targets = {}
        for k, v in preds.items():
            if len(v) > 0:
                out_preds[k] = torch.cat(v).to(device)
            else:
                out_preds[k] = None
        for k, v in targets.items():
            if len(v) > 0:
                out_targets[k] = torch.cat(v).long().to(device)
            else:
                out_targets[k] = None
        return out_preds, out_targets

    def __call__(self, probs: Tensor):
        pass


class ZeroShot:
    def __init__(
        self, bin_times: list[float] = [0.2, 0.4, 0.6, 0.8], frame_hz: int = 50
    ):
        self.bin_frames = [int(t * frame_hz) for t in bin_times]
        self.codebook = Codebook(self.bin_frames)
        self.subsets = {
            "silence": self.init_subset_silence(),
            "prediction": self.init_subset_active(),
            "backchannel": self.init_subset_backchannel(),
        }

    def init_subset_silence(self) -> Tensor:
        """
        During mutual silences we wish to infer which speaker the model deems most likely.

        We focus on classes where only a single speaker is active in the projection window,
        renormalize the probabilities on this subset, and determine which speaker is the most
        likely next speaker.
        """

        # active channel: At least 1 bin is active -> all permutations (all except the no-activity)
        # active = self._all_permutations_mono(n, start=1)  # at least 1 active
        # active channel: At least 1 bin is active -> all permutations (all except the no-activity)
        active = on_activity_change_mono(self.codebook.n_bins, min_active=2)
        # non-active channel: zeros
        non_active = torch.zeros((1, active.shape[-1]))
        # combine
        next_speaker_on_silence_oh = combine_speakers(active, non_active, mirror=True)
        next_speaker_on_silence = self.codebook.encode(next_speaker_on_silence_oh)
        next_speaker_on_silence = sort_idx(next_speaker_on_silence)
        return next_speaker_on_silence

    def init_subset_active(self) -> Tensor:
        """On activity"""
        # Shift subset
        eos = end_of_segment_mono(self.codebook.n_bins, max=2)
        nav = on_activity_change_mono(self.codebook.n_bins, min_active=2)
        shift_oh = combine_speakers(nav, eos, mirror=True)
        shift = self.codebook.encode(shift_oh)
        shift = sort_idx(shift)
        return shift

    def init_subset_backchannel(self, n: int = 4) -> Tensor:
        if n != 4:
            raise NotImplementedError("Not implemented for bin-size != 4")

        # at least 1 bin active over 3 bins
        bc_speaker = all_permutations_mono(n=3, start=1)
        bc_speaker = torch.cat(
            (bc_speaker, torch.zeros((bc_speaker.shape[0], 1))), dim=-1
        )

        # all permutations of 3 bins
        current = all_permutations_mono(n=3, start=0)
        current = torch.cat((current, torch.ones((current.shape[0], 1))), dim=-1)

        bc_both = combine_speakers(bc_speaker, current, mirror=True)
        return self.codebook.encode(bc_both)

    def next_speaker_on_silence_probs(self, probs: Tensor) -> Tensor:
        # Get A/B is next speaker total probability
        pa = probs[..., self.subsets["silence"][0]].sum(-1)
        pb = probs[..., self.subsets["silence"][1]].sum(-1)
        den = pa + pb
        return pa / den

    def next_speaker_on_active_probs(self, probs: Tensor) -> Tensor:
        # Prediction that A is next speaker
        pa = probs[..., self.subsets["prediction"][0]].sum(-1)
        pa_compliment = probs[..., self.subsets["silence"][1]].sum(-1)
        den = pa + pa_compliment
        pa = pa / den

        # Prediction that A is next speaker
        pb = probs[..., self.subsets["prediction"][1]].sum(-1)
        pb_compliment = probs[..., self.subsets["silence"][0]].sum(-1)
        den = pb + pb_compliment
        pb = pb / den
        return torch.stack((pa, pb), dim=-1)

    def backchannel_probs(self, probs: Tensor) -> Tensor:
        bc_b = probs[..., self.subsets["backchannel"][0]].sum(-1)
        bc_a = probs[..., self.subsets["backchannel"][1]].sum(-1)
        return torch.stack((bc_a, bc_b), dim=-1)

    def __call__(self, probs: Tensor):
        ns = self.next_speaker_on_silence_probs(probs)  # B, N
        ps = self.next_speaker_on_active_probs(probs)  # B, N
        bc = self.backchannel_probs(probs)  # B, N, 2
        return {"silence": ns, "prediction": ps, "backchannel": bc}


if __name__ == "__main__":

    import matplotlib.pyplot as plt
    from vap.data.datamodule import VAPDataModule
    from vap.modules.lightning_module import VAPModule
    from vap.utils.plot import plot_melspectrogram, plot_vap_probs, plot_vad

    zs = ZeroShot()
    probs = torch.rand((1, 500, 256))
    zprobs = zs(probs)

    p = "/home/erik/projects/CCConv/VoiceActivityProjection/data/splits/swb/val_20s_5o.csv"
    dm = VAPDataModule(
        train_path=p, val_path=p, num_workers=0, batch_size=15, prefetch_factor=None
    )
    dm.setup()
    dloader = dm.val_dataloader()
    model = VAPModule.load_from_checkpoint("example/checkpoints/checkpoint.ckpt").model
    if torch.cuda.is_available():
        model = model.to("cuda")
    model.eval()

    batch = next(iter(dloader))
    vad = batch["vad"]

    # TODO: Make easy visualization of some prototypical examples. e.g., Phrases.
    for batch in dloader:
        with torch.inference_mode():
            out = model.probs(batch["waveform"].to(model.device))
            zprobs = zs(out["probs"])
        ls = get_last_speaker(vad)
        # b = 6
        for b in range(batch["waveform"].shape[0]):
            # mask_bc_a = torch.logical_not(ls[b, :-100] == 0)
            # mask_bc_b = torch.logical_not(ls[b, :-100] == 1)
            mask_bc_a = ls[b, :-100] == 1
            mask_bc_b = ls[b, :-100] == 0
            fig, ax = plt.subplots(9, 1, sharex=True, figsize=(21, 9))
            plot_melspectrogram(batch["waveform"][b], ax=ax[:2])
            x = torch.arange(batch["vad"][0, :-100].shape[0]) / 50
            plot_vad(x, vad[b, :-100, 0], ax=ax[0], ypad=2)
            plot_vad(x, vad[b, :-100, 1], ax=ax[1], ypad=2)
            plot_vap_probs(
                zprobs["silence"][b].cpu(), ax=ax[2], prob_label="ZS on Silence"
            )
            xx = torch.arange(zprobs["prediction"].shape[1]) / 50
            ax[3].fill_between(
                xx,
                y1=zprobs["prediction"][b, :, 0].cpu(),
                color="blue",
                label="ZS pred A",
                alpha=0.6,
            )
            ax[3].plot(xx, zprobs["prediction"][b, :, 0].cpu(), color="k")
            ax[3].axhline(0.5, color="k", linestyle="--", linewidth=2)
            ax[4].fill_between(
                xx,
                y1=zprobs["prediction"][b, :, 1].cpu(),
                color="orange",
                label="ZS pred B",
                alpha=0.6,
            )
            ax[4].plot(xx, zprobs["prediction"][b, :, 1].cpu(), color="k")
            ax[4].axhline(0.5, color="k", linestyle="--", linewidth=2)
            ax[5].fill_between(
                xx,
                y1=zprobs["backchannel"][b, :, 0].cpu() * mask_bc_a,
                color="blue",
                label="BC A",
                alpha=0.6,
            )
            ax[6].fill_between(
                xx,
                y1=zprobs["backchannel"][b, :, 1].cpu() * mask_bc_b,
                color="orange",
                label="BC B",
                alpha=0.6,
            )
            plot_vap_probs(out["p_now"][b], ax[-2])
            plot_vap_probs(out["p_future"][b], ax[-1], color=["darkblue", "darkorange"])
            for a in ax[2:]:
                a.set_ylim([0, 1])
                a.legend(loc="upper left")
            plt.tight_layout()
            plt.subplots_adjust(hspace=0.05)
            plt.show()
